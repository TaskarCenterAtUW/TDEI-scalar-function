import types

import pytest

import function_app as app


class FakeMessage:
    def __init__(
        self,
        body_parts,
        message_id,
        delivery_count=1,
        application_properties=None,
        raw_application_properties=None,
    ):
        self.body = body_parts
        self.message_id = message_id
        self.delivery_count = delivery_count
        self.application_properties = application_properties or {}
        if raw_application_properties is not None:
            self._raw_amqp_message = types.SimpleNamespace(
                application_properties=raw_application_properties
            )


class FakeReceiver:
    def __init__(self, messages):
        self._messages = messages

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def peek_messages(self, max_message_count):
        return list(self._messages)[:max_message_count]


class FakeServiceBusClient:
    def __init__(self, receiver):
        self._receiver = receiver

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_subscription_receiver(self, topic_name, subscription_name):
        return self._receiver


def _make_config(max_instances=3):
    return app.Config(
        azure=app.AzureProvisioningConfig(
            subscription_id="sub",
            resource_group="rg",
            aci_image="img",
            aci_name_prefix="prefix",
            aci_location="eastus",
            max_instances_per_sub=max_instances,
            default_cpu=2.0,
            memory_multiplier=8.0,
            min_memory_gb=0.5,
            max_memory_gb=10.0,
        ),
        service_bus=app.ServiceBusConfig(
            connection_str="Endpoint=sb://example/",
            namespace_name="ns",
            topic_name="topic",
        ),
        acr=app.AcrConfig(server=None, username=None, password=None),
        instance_env={"CONTAINER_NAME": "osw"},
    )


def test_calculate_memory_clamps_min_max():
    """Category: Memory | Clamp memory to min/max thresholds."""
    config = _make_config()
    assert app._calculate_memory_from_file_size_mb(config, 1) == 0.5
    assert app._calculate_memory_from_file_size_mb(config, 1024) == 8.0
    assert app._calculate_memory_from_file_size_mb(config, 50000) == 10.0


def test_parse_message_application_properties():
    """Category: Message Parsing | Parse file size from application properties."""
    body = [b'{"file_size_mb": 12.5}']
    msg = FakeMessage(
        body,
        message_id="abc",
        application_properties={"file_size_mb": 12.5},
    )
    payload = app._parse_message(msg)
    assert payload == {"message_id": "abc", "file_size_mb": 12.5}


def test_parse_message_bytes_application_properties():
    """Category: Message Parsing | Decode byte keys in application properties."""
    body = [b'{"file_size_mb": 5}']
    msg = FakeMessage(
        body,
        message_id="bytes-key",
        application_properties={b"file_size_mb": 5},
    )
    payload = app._parse_message(msg)
    assert payload == {"message_id": "bytes-key", "file_size_mb": 5.0}


def test_parse_message_raw_amqp_application_properties():
    """Category: Message Parsing | Fallback to raw AMQP properties."""
    body = [b'{"file_size_mb": 7}']
    msg = FakeMessage(
        body,
        message_id="raw-props",
        application_properties={},
        raw_application_properties={b"file_size_mb": 7},
    )
    payload = app._parse_message(msg)
    assert payload == {"message_id": "raw-props", "file_size_mb": 7.0}


def test_parse_message_missing_file_size_in_properties():
    """Category: Message Parsing | Reject missing file size in properties."""
    body = [b'{"data": {"file_size_mb": 2}}']
    msg = FakeMessage(body, message_id="id-2")
    with pytest.raises(ValueError, match="file_size_mb is required"):
        app._parse_message(msg)


def test_parse_message_embedded_json():
    """Category: Message Parsing | Parse JSON embedded in text body."""
    raw = b'prefix {"file_size_mb": 1} suffix'
    msg = FakeMessage(
        [raw],
        message_id="id-3",
        application_properties={"file_size_mb": 1},
    )
    payload = app._parse_message(msg)
    assert payload["file_size_mb"] == 1.0


def test_parse_message_invalid_json():
    """Category: Message Parsing | Reject invalid JSON messages."""
    msg = FakeMessage([b"not json"], message_id="id-4")
    with pytest.raises(ValueError, match="Invalid JSON message"):
        app._parse_message(msg)


def test_parse_message_missing_message_id():
    """Category: Message Parsing | Reject messages without a message_id."""
    msg = FakeMessage(
        [b'{"file_size_mb": 1}'],
        message_id=None,
        application_properties={"file_size_mb": 1},
    )
    with pytest.raises(ValueError, match="message_id is required"):
        app._parse_message(msg)


def test_parse_message_non_numeric_file_size():
    """Category: Message Parsing | Reject non-numeric file size values."""
    msg = FakeMessage(
        [b'{"file_size_mb": "big"}'],
        message_id="id-5",
        application_properties={"file_size_mb": "big"},
    )
    with pytest.raises(ValueError, match="file_size_mb must be a number"):
        app._parse_message(msg)


def test_split_container_groups_terminal_vs_active():
    """Category: Container Groups | Split container groups into active and terminal."""
    succeeded = types.SimpleNamespace(
        instance_view=types.SimpleNamespace(state="Succeeded"),
        provisioning_state="Running",
    )
    running = types.SimpleNamespace(
        instance_view=types.SimpleNamespace(state="Running"),
        provisioning_state="Running",
    )
    failed = types.SimpleNamespace(instance_view=None, provisioning_state="Failed")

    active, terminal = app._split_container_groups([succeeded, running, failed])
    assert running in active
    assert succeeded in terminal
    assert failed in terminal


def test_existing_message_ids_collects_tags():
    """Category: Container Groups | Collect existing message_id tags from groups."""
    group1 = types.SimpleNamespace(tags={"message_id": "a"})
    group2 = types.SimpleNamespace(tags={"message_id": "b"})
    group3 = types.SimpleNamespace(tags={})
    assert app._existing_message_ids([group1, group2, group3]) == {"a", "b"}


def test_provision_from_subscription_respects_max_and_skips_duplicates(monkeypatch):
    """Category: Provisioning | Provision only new messages and respect max count."""
    config = _make_config()
    messages = [
        FakeMessage(
            [b'{"file_size_mb": 1}'],
            message_id="dup",
            application_properties={"file_size_mb": 1},
        ),
        FakeMessage(
            [b'{"file_size_mb": 2}'],
            message_id="new",
            application_properties={"file_size_mb": 2},
        ),
    ]
    receiver = FakeReceiver(messages)
    sb_client = FakeServiceBusClient(receiver)

    created = []

    def fake_create(config, payload, subscription_name):
        created.append((payload["message_id"], subscription_name))
        return "group"

    monkeypatch.setattr(app, "_create_container_instance", fake_create)

    existing = {"dup"}
    provisioned = app._provision_from_subscription(
        config,
        sb_client,
        "subA",
        max_messages=2,
        existing_ids=existing,
        max_delivery_count=10,
    )

    assert provisioned == 1
    assert created == [("new", "subA")]


def test_provision_from_subscription_skips_invalid_messages(monkeypatch):
    """Category: Provisioning | Skip invalid messages and continue provisioning."""
    config = _make_config()
    messages = [
        FakeMessage(
            [b"bad"],
            message_id="a",
            application_properties={"file_size_mb": 1},
        ),
        FakeMessage(
            [b'{"file_size_mb": 1}'],
            message_id="b",
            application_properties={"file_size_mb": 1},
        ),
    ]
    receiver = FakeReceiver(messages)
    sb_client = FakeServiceBusClient(receiver)

    created = []

    def fake_create(config, payload, subscription_name):
        created.append(payload["message_id"])
        return "group"

    monkeypatch.setattr(app, "_create_container_instance", fake_create)

    provisioned = app._provision_from_subscription(
        config,
        sb_client,
        "subA",
        max_messages=2,
        existing_ids=set(),
        max_delivery_count=10,
    )

    assert provisioned == 1
    assert created == ["b"]


def test_scale_subscription_at_capacity(monkeypatch):
    """Category: Scaling | Return at capacity when no slots are available."""
    config = _make_config(max_instances=0)
    monkeypatch.setattr(app, "_get_config", lambda: config)
    group = types.SimpleNamespace(tags={})
    monkeypatch.setattr(app, "_list_relevant_container_groups", lambda _cfg: [group])
    monkeypatch.setattr(
        app, "_split_container_groups", lambda groups: (groups, [])
    )

    result = app._scale_subscription()
    assert result == "topic: at capacity"


def test_scale_subscription_no_subscriptions(monkeypatch):
    """Category: Scaling | Return no subscriptions when topic has none."""
    config = _make_config(max_instances=2)
    monkeypatch.setattr(app, "_get_config", lambda: config)
    monkeypatch.setattr(app, "_list_relevant_container_groups", lambda _cfg: [])
    monkeypatch.setattr(app, "_split_container_groups", lambda groups: ([], []))
    monkeypatch.setattr(app, "_list_topic_subscriptions", lambda _cfg: [])

    class DummySBClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummySBFactory:
        @staticmethod
        def from_connection_string(_):
            return DummySBClient()

    monkeypatch.setattr(app, "ServiceBusClient", DummySBFactory)

    result = app._scale_subscription()
    assert result == "topic: no subscriptions"


def test_scale_subscription_provisions_deterministic_passes(monkeypatch):
    """Category: Scaling | Provision one per subscription in pass 1."""
    config = _make_config(max_instances=2)
    monkeypatch.setattr(app, "_get_config", lambda: config)
    monkeypatch.setattr(app, "_list_relevant_container_groups", lambda _cfg: [])
    monkeypatch.setattr(app, "_split_container_groups", lambda groups: ([], []))
    monkeypatch.delenv("SKIP_SUBSCRIPTIONS", raising=False)
    monkeypatch.setenv("PROVISIONING_MAX_WORKERS", "1")
    monkeypatch.setattr(
        app,
        "_list_topic_subscriptions",
        lambda _cfg: [
            types.SimpleNamespace(name="a", max_delivery_count=10),
            types.SimpleNamespace(name="b", max_delivery_count=10),
        ],
    )

    class DummySBClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummySBFactory:
        @staticmethod
        def from_connection_string(_):
            return DummySBClient()

    monkeypatch.setattr(app, "ServiceBusClient", DummySBFactory)

    calls = []

    def fake_provision(
        config,
        sb_client,
        subscription_name,
        max_messages,
        existing_ids,
        max_delivery_count,
        existing_ids_lock=None,
        limiter=None,
    ):
        calls.append((subscription_name, max_messages))
        return 1

    monkeypatch.setattr(app, "_provision_from_subscription", fake_provision)

    result = app._scale_subscription()
    assert result == "topic: OK"
    assert sorted(calls) == [("a", 1), ("b", 1)]
