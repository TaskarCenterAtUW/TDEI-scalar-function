# ACI Scale Management

Timer-triggered Azure Function that peeks Service Bus topic subscriptions and
provisions Azure Container Instances (ACI) sized by message `file_size_mb`
from Service Bus application properties.

## Quick start
1. Create a virtual environment and install dependencies:
   - `python -m venv .venv`
   - `source .venv/bin/activate`
   - `pip install -r requirements.txt`
2. Configure environment variables (see `.env`).
3. Run tests:
   - Unit tests: `pytest -m "not integration" -vv`
   - Integration test: `pytest -m integration -vv`

## Environment variables
See `.env` for a full, working example. Key groups:

- Azure: `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`
- ACI: `ACI_IMAGE`, `ACI_NAME_PREFIX`, `ACI_LOCATION`,
  `ACI_MAX_INSTANCES`, `ACI_DEFAULT_CPU`, `ACI_MEMORY_MULTIPLIER`,
  `ACI_MIN_MEMORY_GB`, `ACI_MAX_MEMORY_GB`
- ACR (optional): `ACR_SERVER`, `ACR_USERNAME`, `ACR_PASSWORD`
- Service Bus:
  - `SB_CONNECTION_STR` (required)
  - `SB_NAMESPACE` (optional if derivable from connection string)
  - `SB_TOPIC_NAME` (required)
- Optional processing control:
  - `SKIP_SUBSCRIPTIONS` (comma-separated subscription names to skip)
  - `PROVISIONING_MAX_WORKERS` (max parallel workers for provisioning, default 4)
- Container env pass-through:
  - Set any `INSTANCE_*` variables and they will be passed to the container
    without the `INSTANCE_` prefix.
  - Optional: `INSTANCE_SUBSCRIPTION_ENV_NAME` sets which env key receives the
    subscription name (e.g. `VALIDATION_REQ_SUB`).

## Service Bus message format
Only `message_id` (from Service Bus metadata) and `file_size_mb` are required.
`file_size_mb` must be supplied as a Service Bus application property
(`application_properties.file_size_mb`).

Example:
```json
{
  "messageId": "5e1a464d-9d69-4e74-871b-474bdc31da20",
  "messageType": "osw_validation_only|osw_validation_only",
  "publishedDate": "2025-03-20T13:18:42.501Z",
  "data": {}
}
```

## How it works
- Lists container groups tagged with `managed_by = ACI_NAME_PREFIX`.
- Splits into active vs terminal groups.
- Skips provisioning if a `message_id` already exists in active or terminal
  container tags.
- Peeks messages (does not settle them).
- Provisions in parallel across subscriptions (bounded by `PROVISIONING_MAX_WORKERS`).
- Filters out subscriptions listed in `SKIP_SUBSCRIPTIONS`.
- Creates an ACI group per message with tags:
  `managed_by`, `message_id`, `file_size_mb`.
- Deletes terminal containers after provisioning (state `Terminated` or `Failed`).
- Captures the last 20 log lines from each container before deletion.

## Integration test
The integration test sends one or more messages to the topic (file sizes are
passed as Service Bus application properties), waits for provisioning, verifies
memory sizing, waits for terminal state, then confirms container deletion.

Configure the message template:
- `TEST_MESSAGE_JSON_PATH` (default: `tests/data/integration_message.json`)

Run:
- `pytest -m integration -vv`
- `pytest -m "not integration" -vv`

- `pytest -m integration tests/test_integration_e2e.py --e2e-file-sizes 50,180,500,1024 --e2e-expected-subscriptions 1-50MB,51-200MB,201-600MB,601-1GB --e2e-timeout-seconds 900`

## TODO
- GitWorkflow strategy for provisioning for different services
  - Right now manual deployment can be achived
