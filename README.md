# ACI Scale Management

Timer-triggered Azure Function that peeks Service Bus topic subscriptions and
provisions Azure Container Instances (ACI) sized by message `file_size_mb`.

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
- Container env pass-through:
  - Set any `INSTANCE_*` variables and they will be passed to the container
    without the `INSTANCE_` prefix.
  - Optional: `INSTANCE_SUBSCRIPTION_ENV_NAME` sets which env key receives the
    subscription name (e.g. `VALIDATION_REQ_SUB`).

## Service Bus message format
Only `message_id` (from Service Bus metadata) and `file_size_mb` are required.
`file_size_mb` may be at the top-level or under `data.file_size_mb`.

Example:
```json
{
  "messageId": "5e1a464d-9d69-4e74-871b-474bdc31da20",
  "messageType": "osw_validation_only|osw_validation_only",
  "publishedDate": "2025-03-20T13:18:42.501Z",
  "data": {
    "file_size_mb": 50
  }
}
```

## How it works
- Lists container groups tagged with `managed_by = ACI_NAME_PREFIX`.
- Splits into active vs terminal groups.
- Skips provisioning if a `message_id` already exists in active or terminal
  container tags.
- Skips provisioning when `delivery_count` is at/above the subscription
  `max_delivery_count`.
- Peeks messages (does not settle them).
- Creates an ACI group per message with tags:
  `managed_by`, `message_id`, `file_size_mb`.
- Deletes terminal containers after provisioning.

## Integration test
The integration test sends a message to the topic, waits for provisioning,
verifies memory sizing, waits for terminal state, then confirms container
deletion and message clearance.

Configure the message template:
- `TEST_MESSAGE_JSON_PATH` (default: `tests/data/integration_message.json`)

Run:
- `pytest -m integration -vv`

