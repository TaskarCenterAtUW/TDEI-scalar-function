import os
import logging
import uuid
import json
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import azure.functions as func
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError
from azure.core.exceptions import AzureError
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.servicebus import ServiceBusManagementClient
from azure.mgmt.containerinstance.models import (
    ContainerGroup,
    Container,
    ContainerPort,
    Port,
    IpAddress,
    ResourceRequests,
    ResourceRequirements,
    OperatingSystemTypes,
    EnvironmentVariable,
    ImageRegistryCredential,
    ContainerGroupRestartPolicy,
)

load_dotenv()
app = func.FunctionApp()


@dataclass(frozen=True)
class AzureProvisioningConfig:
    subscription_id: str
    resource_group: str
    aci_image: str
    aci_name_prefix: str
    aci_location: str
    max_instances_per_sub: int
    default_cpu: float
    memory_multiplier: float
    min_memory_gb: float
    max_memory_gb: float


@dataclass(frozen=True)
class ServiceBusConfig:
    connection_str: str
    namespace_name: str
    topic_name: str


@dataclass(frozen=True)
class AcrConfig:
    server: Optional[str]
    username: Optional[str]
    password: Optional[str]


@dataclass(frozen=True)
class Config:
    azure: AzureProvisioningConfig
    service_bus: ServiceBusConfig
    acr: AcrConfig
    instance_env: Dict[str, str]


# SDK clients (lazily initialized to avoid startup timeout)
_credential = None
_aci_client = None
_config = None
_sb_mgmt_client = None


def _get_env(name: str) -> Optional[str]:
    return os.environ.get(name)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(f"Missing required env var: {name}")
    return value


def _derive_sb_namespace(connection_str: str) -> Optional[str]:
    for part in connection_str.split(";"):
        if part.startswith("Endpoint="):
            endpoint = part.split("=", 1)[1].strip()
            if "://" in endpoint:
                endpoint = endpoint.split("://", 1)[1]
            host = endpoint.split("/", 1)[0]
            if host.endswith(".servicebus.windows.net"):
                return host.split(".", 1)[0]
            return host.split(".", 1)[0] if host else None
    return None


def _get_instance_env() -> Dict[str, str]:
    instance_env = {}
    for name, value in os.environ.items():
        if name.startswith("INSTANCE_"):
            instance_env[name[len("INSTANCE_") :]] = value
    return instance_env


def _build_service_bus_config() -> ServiceBusConfig:
    connection_str = _require_env("SB_CONNECTION_STR")
    namespace_name = _get_env("SB_NAMESPACE") or _derive_sb_namespace(connection_str)
    if not namespace_name:
        raise EnvironmentError(
            "Missing required env var: SB_NAMESPACE (unable to derive from SB_CONNECTION_STR)"
        )
    return ServiceBusConfig(
        connection_str=connection_str,
        namespace_name=namespace_name,
        topic_name=_get_env("SB_TOPIC_NAME"),
    )


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = Config(
            azure=AzureProvisioningConfig(
                subscription_id=_require_env("AZURE_SUBSCRIPTION_ID"),
                resource_group=_require_env("AZURE_RESOURCE_GROUP"),
                aci_image=_require_env("ACI_IMAGE"),
                aci_name_prefix=_require_env("ACI_NAME_PREFIX"),
                aci_location=_require_env("ACI_LOCATION"),
                max_instances_per_sub=int(_get_env("ACI_MAX_INSTANCES") or "200"),
                default_cpu=float(_get_env("ACI_DEFAULT_CPU") or "2.0"),
                memory_multiplier=float(_get_env("ACI_MEMORY_MULTIPLIER") or "8.0"),
                min_memory_gb=float(_get_env("ACI_MIN_MEMORY_GB") or "0.5"),
                max_memory_gb=float(_get_env("ACI_MAX_MEMORY_GB") or "240.0"),
            ),
            service_bus=_build_service_bus_config(),
            acr=AcrConfig(
                server=_get_env("ACR_SERVER"),
                username=_get_env("ACR_USERNAME"),
                password=_get_env("ACR_PASSWORD"),
            ),
            instance_env=_get_instance_env(),
        )
    return _config


def _log_config_summary(config: Config) -> None:
    logging.info("Config categories and required envs:")
    logging.info(
        "Azure provisioning (required): AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, "
        "ACI_IMAGE, ACI_NAME_PREFIX, ACI_LOCATION"
    )
    logging.info(
        "Azure provisioning (optional defaults): ACI_MAX_INSTANCES, ACI_DEFAULT_CPU, "
        "ACI_MEMORY_MULTIPLIER, ACI_MIN_MEMORY_GB, ACI_MAX_MEMORY_GB"
    )
    logging.info(
        "Service Bus (required): SB_CONNECTION_STR, "
        "SB_NAMESPACE (optional if derivable)"
    )
    logging.info("Instance service (pass-through): INSTANCE_*")
    logging.info("ACR (optional): ACR_SERVER, ACR_USERNAME, ACR_PASSWORD")
    logging.info("Instance connections: INSTANCE_*")


def _get_credential():
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _get_aci_client():
    global _aci_client
    if _aci_client is None:
        config = _get_config()
        _aci_client = ContainerInstanceManagementClient(
            _get_credential(), config.azure.subscription_id
        )
    return _aci_client


def _get_sb_mgmt_client():
    global _sb_mgmt_client
    if _sb_mgmt_client is None:
        config = _get_config()
        _sb_mgmt_client = ServiceBusManagementClient(
            _get_credential(), config.azure.subscription_id
        )
    return _sb_mgmt_client


def _list_topic_subscriptions(config: Config):
    client = _get_sb_mgmt_client()
    return list(
        client.subscriptions.list_by_topic(
            config.azure.resource_group,
            config.service_bus.namespace_name,
            config.service_bus.topic_name,
        )
    )


def _calculate_memory_from_file_size_mb(config: Config, file_size_mb: float) -> float:
    """Calculate memory requirements based on file size in MB.

    Converts MB to GB first, then multiplies by the multiplier.
    E.g., with MEMORY_MULTIPLIER=8.0: 500MB = 0.488GB -> 3.9GB memory
    """
    file_size_gb = file_size_mb / 1024
    memory_gb = config.azure.memory_multiplier * file_size_gb
    return max(config.azure.min_memory_gb, min(memory_gb, config.azure.max_memory_gb))


def _list_relevant_container_groups(config: Config):
    """List container groups in the resource group that were created for this application.

    We identify groups by a tag `managed_by` set to the ACI name prefix.
    """
    all_groups = list(
        _get_aci_client().container_groups.list_by_resource_group(
            config.azure.resource_group
        )
    )
    relevant = [
        g
        for g in all_groups
        if g.tags and g.tags.get("managed_by") == config.azure.aci_name_prefix
    ]
    return relevant


def _get_container_state(cg: ContainerGroup) -> str:
    try:
        if cg.instance_view and getattr(cg.instance_view, "state", None):
            return cg.instance_view.state
    except Exception:
        pass
    return getattr(cg, "provisioning_state", "Unknown")


def _split_container_groups(
    groups: Iterable[ContainerGroup],
) -> Tuple[List[ContainerGroup], List[ContainerGroup]]:
    terminal_states = {"Succeeded", "Terminated", "Failed"}
    active = []
    terminal = []
    for group in groups:
        state = _get_container_state(group)
        if state in terminal_states:
            terminal.append(group)
        else:
            active.append(group)
    return active, terminal


def _existing_message_ids(groups: Iterable[ContainerGroup]) -> set:
    return {
        g.tags.get("message_id") for g in groups if g.tags and g.tags.get("message_id")
    }


def _provision_from_subscription(
    config: Config,
    sb_client: ServiceBusClient,
    subscription_name: str,
    max_messages: int,
    existing_ids: set,
    max_delivery_count: Optional[int] = None,
) -> int:
    if max_messages <= 0:
        return 0
    if not subscription_name:
        logging.warning("Skipping subscription with missing name")
        return 0
    receiver = sb_client.get_subscription_receiver(
        topic_name=config.service_bus.topic_name,
        subscription_name=subscription_name,
    )
    provisioned = 0
    with receiver:
        messages = receiver.peek_messages(max_message_count=max_messages)
        if not messages:
            return 0
        for msg in messages:
            try:
                # if max_delivery_count is not None:
                #     delivery_count = getattr(msg, "delivery_count", None)
                #     if delivery_count is not None and delivery_count >= max_delivery_count:
                #         logging.warning(
                #             "Skipping message %s: delivery_count=%s reached max=%s",
                #             msg.message_id,
                #             delivery_count,
                #             max_delivery_count,
                #         )
                #         continue
                payload = _parse_message(msg)
                if payload["message_id"] in existing_ids:
                    logging.info(
                        "Message %s already provisioned, skipping",
                        payload["message_id"],
                    )
                    continue
                _create_container_instance(config, payload, subscription_name)
                existing_ids.add(payload["message_id"])
                provisioned += 1
                if provisioned >= max_messages:
                    break
            except ValueError as exc:
                logging.warning("Invalid message payload, skipping: %s", exc)
            except AzureError as exc:
                logging.exception("ACI provisioning error while provisioning: %s", exc)
            except Exception as exc:
                logging.exception("Unexpected error while provisioning: %s", exc)
    return provisioned


def _parse_message(msg) -> dict:
    try:
        body_parts = list(msg.body)
        body_bytes = b"".join(
            [p if isinstance(p, bytes) else str(p).encode() for p in body_parts]
        )
        raw_text = body_bytes.decode("utf-8")
        try:
            message_data = json.loads(raw_text)
        except json.JSONDecodeError:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start == -1 or end == -1 or start >= end:
                raise
            message_data = json.loads(raw_text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON message: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Unable to read message body: {exc}") from exc

    data = message_data.get("data") or {}
    message_id = msg.message_id
    if not message_id:
        raise ValueError("message_id is required")

    file_size_raw = message_data.get("file_size_mb")
    if file_size_raw is None:
        file_size_raw = data.get("file_size_mb")
    try:
        file_size_mb = float(file_size_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("file_size_mb must be a number") from exc

    return {
        "message_id": message_id,
        "file_size_mb": file_size_mb,
    }


def _build_container_env(
    config: Config, source_subscription: str
) -> List[EnvironmentVariable]:
    env_vars = []
    subscription_env_name = config.instance_env.get("SUBSCRIPTION_ENV_NAME")
    for name, value in config.instance_env.items():
        if name == "SUBSCRIPTION_ENV_NAME":
            continue
        env_vars.append(EnvironmentVariable(name=name, value=str(value)))
    if subscription_env_name and source_subscription:
        env_vars.append(
            EnvironmentVariable(
                name=subscription_env_name,
                value=str(source_subscription),
            )
        )
    return env_vars


def _create_container_instance(
    config: Config, payload: dict, source_subscription: str
) -> str:
    """Create an Azure Container Instance configured to process the request.

    The container receives the message details via environment variables.
    """
    group_name = f"{config.azure.aci_name_prefix}-{uuid.uuid4().hex[:8]}"

    # Calculate memory based on file size
    memory_gb = _calculate_memory_from_file_size_mb(config, payload["file_size_mb"])
    cpu = config.azure.default_cpu

    logging.info(
        "Creating container for message %s: file_size=%sMB, memory=%sGB, cpu=%s",
        payload["message_id"],
        payload["file_size_mb"],
        memory_gb,
        cpu,
    )
    resources = ResourceRequirements(
        requests=ResourceRequests(memory_in_gb=memory_gb, cpu=cpu)
    )

    # Pass validation runner configuration to the container
    env_vars = _build_container_env(config, source_subscription)

    container = Container(
        name=group_name,
        image=config.azure.aci_image,
        resources=resources,
        environment_variables=env_vars,
        ports=[
            ContainerPort(port=80, protocol="TCP")
        ],  # Expose port 80 for potential health checks or communication
    )

    # Add registry credentials if provided (for private ACR)
    image_registry_credentials = None
    if config.acr.server and config.acr.username and config.acr.password:
        image_registry_credentials = [
            ImageRegistryCredential(
                server=config.acr.server,
                username=config.acr.username,
                password=config.acr.password,
            )
        ]
        logging.info("Using ACR credentials for server: %s", config.acr.server)

    group = ContainerGroup(
        location=config.azure.aci_location,
        containers=[container],
        os_type=OperatingSystemTypes.linux,
        restart_policy=ContainerGroupRestartPolicy.NEVER,
        ip_address=IpAddress(
            ports=[Port(protocol="TCP", port=80)],  # External exposed port
            type="Public",
        ),
        image_registry_credentials=image_registry_credentials,
        tags={
            "managed_by": config.azure.aci_name_prefix,
            "message_id": payload["message_id"],
            "file_size_mb": str(payload["file_size_mb"]),
        },
    )

    logging.info(
        f"Creating container group {group_name} (cpu={cpu}, mem={memory_gb}GB)"
    )
    poller = _get_aci_client().container_groups.begin_create_or_update(
        config.azure.resource_group, group_name, group
    )
    # poller.result()
    while not poller.done():
        logging.info(f"Provisioning ACI {group_name}... Status: {poller.status()}")
        time.sleep(10)  # Check every 10 seconds

    if poller.status() == "Succeeded":
        logging.info(f"Successfully provisioned {group_name}")
    else:
        logging.error(f"Provisioning failed with status: {poller.status()}")

    return group_name


def _delete_container_group(config: Config, name: str):
    logging.info(f"Deleting container group {name}")
    poller = _get_aci_client().container_groups.begin_delete(
        config.azure.resource_group, name
    )
    try:
        poller.wait() if hasattr(poller, "wait") else poller.result()
    except Exception as e:
        logging.exception(f"Error deleting container group {name}: {e}")


def _scale_subscription():
    """Scale logic:
    - Receive messages
    - Create container per message (bounded by MAX_INSTANCES_PER_SUB)
    - Delete succeeded/terminated containers
    """
    config = None
    try:
        config = _get_config()
        logging.info(
            "[%s] Starting _scale_subscription",
            config.service_bus.topic_name,
        )

        # 1) List existing container groups
        logging.info(
            "[%s] Listing existing containers...",
            config.service_bus.topic_name,
        )
        groups = _list_relevant_container_groups(config)
        logging.info(
            "[%s] Found %s total container groups",
            config.service_bus.topic_name,
            len(groups),
        )

        active_groups, terminal_groups = _split_container_groups(groups)
        logging.info(
            "[%s] Active containers: %s, terminal containers: %s",
            config.service_bus.topic_name,
            len(active_groups),
            len(terminal_groups),
        )

        existing_ids = _existing_message_ids(groups)

        # 2) Peek messages by subscription and spin containers
        try:
            sb_client = ServiceBusClient.from_connection_string(
                config.service_bus.connection_str
            )
            with sb_client:
                available_slots = config.azure.max_instances_per_sub - len(
                    active_groups
                )
                if available_slots <= 0:
                    logging.info(
                        "[%s] No capacity available (max=%s, active=%s)",
                        config.service_bus.topic_name,
                        config.azure.max_instances_per_sub,
                        len(active_groups),
                    )
                    return f"{config.service_bus.topic_name}: at capacity"

                subscriptions = sorted(
                    _list_topic_subscriptions(config),
                    key=lambda sub: sub.name,
                )
                if not subscriptions:
                    logging.info(
                        "[%s] No subscriptions found to process",
                        config.service_bus.topic_name,
                    )
                    return f"{config.service_bus.topic_name}: no subscriptions"

                remaining_slots = available_slots
                # Deterministic pass 1: one message per subscription (sorted by name).
                for subscription in subscriptions:
                    if remaining_slots <= 0:
                        break
                    if not getattr(subscription, "name", None):
                        logging.warning(
                            "Skipping subscription with missing name: %s",
                            getattr(subscription, "__dict__", subscription),
                        )
                        continue
                    provisioned = _provision_from_subscription(
                        config,
                        sb_client,
                        subscription.name,
                        1,
                        existing_ids,
                        getattr(subscription, "max_delivery_count", None),
                    )
                    remaining_slots -= provisioned

                # Deterministic pass 2: fill remaining slots in the same order.
                if remaining_slots > 0:
                    for subscription in subscriptions:
                        if remaining_slots <= 0:
                            break
                        if not getattr(subscription, "name", None):
                            logging.warning(
                                "Skipping subscription with missing name: %s",
                                getattr(subscription, "__dict__", subscription),
                            )
                            continue
                        provisioned = _provision_from_subscription(
                            config,
                            sb_client,
                            subscription.name,
                            remaining_slots,
                            existing_ids,
                            getattr(subscription, "max_delivery_count", None),
                        )
                        remaining_slots -= provisioned

        except ServiceBusError as exc:
            logging.exception(
                "Error peeking messages for %s: %s",
                config.service_bus.topic_name,
                exc,
            )
        except Exception as exc:
            logging.exception(
                "Unhandled error peeking messages for %s: %s",
                config.service_bus.topic_name,
                exc,
            )

        # 3) Delete all terminal containers to clean up (after provisioning)
        if terminal_groups:
            logging.info("Deleting %s terminal containers", len(terminal_groups))
            for group in terminal_groups:
                try:
                    _delete_container_group(config, group.name)
                except Exception as exc:
                    logging.exception(
                        "Failed to delete terminal container %s: %s",
                        group.name,
                        exc,
                    )

    except Exception as e:
        subscription_name = config.service_bus.topic_name if config else "unknown-topic"
        logging.error(
            "[%s] Fatal error in _scale_subscription: %s", subscription_name, e
        )
        logging.exception(e)
        raise

    logging.info(
        "[%s] _scale_subscription completed successfully",
        config.service_bus.topic_name,
    )
    return f"{config.service_bus.topic_name}: OK"


@app.timer_trigger(schedule="0 */1 * * * *", arg_name="mytimer")
def main(mytimer: func.TimerRequest, context: func.Context) -> None:
    logging.info("===== SCALER TRIGGERED - STARTING EXECUTION - v2 =====")
    config = _get_config()
    _log_config_summary(config)

    # Run scaling logic
    try:
        result = _scale_subscription()
        logging.info("Scaling completed successfully: %s", result)
    except Exception as e:
        logging.error(f"Scaling failed with exception: {e}")
        logging.exception(e)
        raise

    logging.info("===== SCALER EXECUTION COMPLETED =====")
