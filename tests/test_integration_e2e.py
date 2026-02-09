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


def _poll_interval():
    return 10.0


def _global_timeout():
    return 600.0


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


def _wait_for_group_by_message_id(config, message_id, deadline):
    while time.monotonic() < deadline:
        groups = app._list_relevant_container_groups(config)
        for group in groups:
            if group.tags and group.tags.get("message_id") == message_id:
                return group
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            break
        time.sleep(min(_poll_interval(), remaining))
    return None


def _wait_for_terminal_state(config, group_name, deadline):
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
        time.sleep(min(_poll_interval(), remaining))
    return None


def _wait_for_group_deleted(config, group_name, deadline):
    while time.monotonic() < deadline:
        groups = app._list_relevant_container_groups(config)
        if not any(g.name == group_name for g in groups):
            return True
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            break
        time.sleep(min(_poll_interval(), remaining))
    return False


def _wait_for_message_cleared(config, message_id, deadline):
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
        time.sleep(min(_poll_interval(), remaining))
    return False


@pytest.mark.integration
def test_end_to_end_happy_flow():
    missing = _missing_env_vars()
    if missing:
        pytest.skip(f"Missing required env vars: {', '.join(missing)}")

    LOGGER.info("Starting integration test: end-to-end happy flow")
    _reset_app_state()
    config = app._get_config()

    message_id = f"e2e-{uuid.uuid4()}"
    message_path = os.environ.get(
        "TEST_MESSAGE_JSON_PATH",
        "tests/fixtures/integration_message.json",
    )
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

    file_size_mb = _extract_file_size_mb(message_body)
    LOGGER.info("Sending test message to topic: %s", config.service_bus.topic_name)
    sb_client = ServiceBusClient.from_connection_string(
        config.service_bus.connection_str
    )
    with sb_client:
        sender = sb_client.get_topic_sender(config.service_bus.topic_name)
        with sender:
            body = json.dumps(message_body)
            message = ServiceBusMessage(body=body, message_id=message_id)
            sender.send_messages(message)
    LOGGER.info("Message sent. message_id=%s", message_id)

    group_name = None
    deadline = time.monotonic() + _global_timeout()
    try:
        LOGGER.info("Triggering scale logic")
        app._scale_subscription()

        group = _wait_for_group_by_message_id(
            config, message_id, deadline
        )
        assert group is not None, "Container group was not created"
        group_name = group.name
        LOGGER.info("Container group created: %s", group_name)

        expected_memory = app._calculate_memory_from_file_size_mb(
            config, file_size_mb
        )
        actual_memory = group.containers[0].resources.requests.memory_in_gb
        assert actual_memory == pytest.approx(expected_memory, rel=1e-3)
        assert group.tags.get("file_size_mb") == str(file_size_mb)
        LOGGER.info(
            "Memory check OK. expected=%.3fGB actual=%.3fGB",
            expected_memory,
            actual_memory,
        )

        terminal_group = _wait_for_terminal_state(
            config, group_name, deadline
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

        deleted = _wait_for_group_deleted(
            config, group_name, deadline
        )
        if not deleted:
            LOGGER.error("Container group was not deleted after completion")
        else:
            LOGGER.info("Container group deleted: %s", group_name)
        assert deleted, "Container group was not deleted after completion"

        cleared = _wait_for_message_cleared(config, message_id, deadline)
        if not cleared:
            LOGGER.error("Message was not cleared from subscriptions")
        else:
            LOGGER.info("Message cleared from subscriptions")
        assert cleared, "Message was not cleared from subscriptions"
    finally:
        if group_name:
            try:
                LOGGER.info("Ensuring container group cleanup: %s", group_name)
                app._delete_container_group(config, group_name)
            except Exception:
                pass
