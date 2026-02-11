import copy
import json
import logging
import os
import time
import uuid

import pytest
from azure.servicebus import ServiceBusClient, ServiceBusMessage

import function_app as app

LOGGER = logging.getLogger(__name__)

REQUIRED_ENV_VARS = [
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_RESOURCE_GROUP",
    "ACI_IMAGE",
    "ACI_NAME_PREFIX",
    "ACI_LOCATION",
    "SB_CONNECTION_STR",
    "SB_TOPIC_NAME",
]


def _missing_env_vars():
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def _reset_app_state():
    app._config = None
    app._aci_client = None
    app._sb_mgmt_client = None
    app._credential = None


def _silence_azure_sdk_logs():
    for name in ("azure", "azure.core", "azure.identity", "azure.servicebus"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _global_timeout(default_seconds: float) -> float:
    return default_seconds


def _remaining_time(deadline):
    return max(0.0, deadline - time.monotonic())


def _extract_file_size_mb(message_body):
    file_size_raw = message_body.get("file_size_mb")
    if file_size_raw is None:
        data = message_body.get("data") or {}
        file_size_raw = data.get("file_size_mb")
    if file_size_raw is None:
        raise ValueError("Message body missing file_size_mb")
    return float(file_size_raw)


def _find_group_by_message_id(config, message_id):
    groups = app._list_relevant_container_groups(config)
    for group in groups:
        if group.tags and group.tags.get("message_id") == message_id:
            return group
    return None


def _wait_for_group_by_message_id(config, message_id, deadline, poll_interval):
    while time.monotonic() < deadline:
        group = _find_group_by_message_id(config, message_id)
        if group:
            return group
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return None


def _wait_for_terminal_state(config, group_name, deadline, poll_interval):
    terminal_states = {"Succeeded", "Terminated", "Failed"}
    while time.monotonic() < deadline:
        groups = app._list_relevant_container_groups(config)
        group = next((g for g in groups if g.name == group_name), None)
        if not group:
            return None
        if app._get_container_state(group) in terminal_states:
            return group
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return None


def _wait_for_group_deleted(config, group_name, deadline, poll_interval):
    while time.monotonic() < deadline:
        groups = app._list_relevant_container_groups(config)
        if not any(g.name == group_name for g in groups):
            return True
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return False


def _wait_for_message_cleared(config, message_id, deadline, poll_interval):
    while time.monotonic() < deadline:
        found = False
        subscriptions = app._list_topic_subscriptions(config)
        sb_client = ServiceBusClient.from_connection_string(
            config.service_bus.connection_str
        )
        with sb_client:
            for subscription in subscriptions:
                receiver = sb_client.get_subscription_receiver(
                    topic_name=config.service_bus.topic_name,
                    subscription_name=subscription.name,
                )
                with receiver:
                    messages = receiver.peek_messages(max_message_count=50)
                    if any(msg.message_id == message_id for msg in messages):
                        found = True
                        break
        if not found:
            return True
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return False


def _find_message_subscriptions(config, message_id):
    subscriptions = app._list_topic_subscriptions(config)
    sb_client = ServiceBusClient.from_connection_string(config.service_bus.connection_str)
    found = []
    with sb_client:
        for subscription in subscriptions:
            receiver = sb_client.get_subscription_receiver(
                topic_name=config.service_bus.topic_name,
                subscription_name=subscription.name,
            )
            with receiver:
                messages = receiver.peek_messages(max_message_count=50)
                if any(msg.message_id == message_id for msg in messages):
                    found.append(subscription.name)
    return found


def _wait_for_message_routed(
    config, message_id, deadline, poll_interval, expected_subscription=None
):
    while time.monotonic() < deadline:
        found = _find_message_subscriptions(config, message_id)
        if expected_subscription:
            if expected_subscription in found:
                return found
        elif found:
            return found
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return []


def _parse_csv_floats(raw):
    if not raw:
        return []
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return [float(part) for part in parts]


def _parse_csv_strings(raw):
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _build_message_payload(template, file_size_mb):
    payload = copy.deepcopy(template)
    return payload


@pytest.mark.integration
def test_end_to_end_happy_flow(pytestconfig):
    missing = _missing_env_vars()
    if missing:
        pytest.skip(f"Missing required env vars: {', '.join(missing)}")

    LOGGER.info("Starting integration test: end-to-end happy flow")
    _silence_azure_sdk_logs()
    _reset_app_state()
    config = app._get_config()

    message_path = (
        os.environ.get("TEST_MESSAGE_JSON_PATH")
        or pytestconfig.getoption("--e2e-message-template")
    )
    file_sizes_raw = pytestconfig.getoption("--e2e-file-sizes")
    expected_subscriptions_raw = pytestconfig.getoption(
        "--e2e-expected-subscriptions"
    )
    timeout_seconds = pytestconfig.getoption("--e2e-timeout-seconds")
    poll_interval = pytestconfig.getoption("--e2e-poll-interval-seconds")
    LOGGER.info("Loading test message template: %s", message_path)
    if not os.path.isfile(message_path):
        raise FileNotFoundError(
            f"TEST_MESSAGE_JSON_PATH not found: {message_path}"
        )
    with open(message_path, "r", encoding="utf-8") as handle:
        message_body_raw = handle.read()
    try:
        message_body = json.loads(message_body_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid TEST_MESSAGE_JSON_PATH content: {exc}") from exc

    file_sizes = _parse_csv_floats(file_sizes_raw)
    if not file_sizes:
        file_sizes = [_extract_file_size_mb(message_body)]
    expected_subscriptions = _parse_csv_strings(expected_subscriptions_raw)
    if expected_subscriptions and len(expected_subscriptions) != len(file_sizes):
        raise ValueError(
            "Expected subscriptions must match file sizes count "
            f"({len(expected_subscriptions)} != {len(file_sizes)})"
        )
    if len(file_sizes) > 1 and not expected_subscriptions:
        raise ValueError(
            "Provide --e2e-expected-subscriptions when using multiple file sizes"
        )

    LOGGER.info(
        "Sending %s test messages to topic: %s",
        len(file_sizes),
        config.service_bus.topic_name,
    )
    sb_client = ServiceBusClient.from_connection_string(
        config.service_bus.connection_str
    )
    message_ids = []
    with sb_client:
        sender = sb_client.get_topic_sender(config.service_bus.topic_name)
        with sender:
            for idx, file_size_mb in enumerate(file_sizes):
                message_id = f"e2e-{uuid.uuid4()}"
                payload = _build_message_payload(message_body, file_size_mb)
                payload["messageId"] = message_id
                body = json.dumps(payload)
                message = ServiceBusMessage(
                    body=body,
                    message_id=message_id,
                    application_properties={"file_size_mb": file_size_mb},
                )
                sender.send_messages(message)
                message_ids.append(message_id)
                LOGGER.info(
                    "Message sent. message_id=%s file_size_mb=%s",
                    message_id,
                    file_size_mb,
                )

    deadline = time.monotonic() + _global_timeout(timeout_seconds)
    if expected_subscriptions:
        for message_id, expected_subscription in zip(
            message_ids, expected_subscriptions
        ):
            found = _wait_for_message_routed(
                config, message_id, deadline, poll_interval, expected_subscription
            )
            assert (
                expected_subscription in found
            ), f"Message {message_id} not found in expected subscription"
            assert found == [
                expected_subscription
            ], f"Message {message_id} routed to unexpected subscriptions: {found}"

    group_names = []
    try:
        LOGGER.info("Triggering scale logic")
        app._scale_subscription()

        groups_by_message = {}
        for message_id in message_ids:
            group = _wait_for_group_by_message_id(
                config, message_id, deadline, poll_interval
            )
            assert group is not None, f"Container group not created for {message_id}"
            groups_by_message[message_id] = group
            group_names.append(group.name)

        for message_id, file_size_mb in zip(message_ids, file_sizes):
            group = groups_by_message[message_id]
            expected_memory = app._calculate_memory_from_file_size_mb(
                config, file_size_mb
            )
            actual_memory = group.containers[0].resources.requests.memory_in_gb
            assert actual_memory == pytest.approx(expected_memory, rel=1e-3)
            assert group.tags.get("file_size_mb") == str(file_size_mb)
            assert group.tags.get("message_id") == message_id
            LOGGER.info(
                "Memory check OK. message_id=%s expected=%.3fGB actual=%.3fGB",
                message_id,
                expected_memory,
                actual_memory,
            )

        for group_name in group_names:
            terminal_group = _wait_for_terminal_state(
                config, group_name, deadline, poll_interval
            )
            if terminal_group is None:
                LOGGER.error("Container group did not reach terminal state")
            else:
                LOGGER.info("Container group reached terminal state")
            assert (
                terminal_group is not None
            ), "Container group did not reach terminal state"

        LOGGER.info("Triggering scale logic for cleanup")
        app._scale_subscription()

        for group_name in group_names:
            deleted = _wait_for_group_deleted(
                config, group_name, deadline, poll_interval
            )
            if not deleted:
                LOGGER.error("Container group was not deleted after completion")
            else:
                LOGGER.info("Container group deleted: %s", group_name)
            assert deleted, "Container group was not deleted after completion"

    finally:
        for group_name in group_names:
            try:
                LOGGER.info("Ensuring container group cleanup: %s", group_name)
                app._delete_container_group(config, group_name)
            except Exception:
                pass
