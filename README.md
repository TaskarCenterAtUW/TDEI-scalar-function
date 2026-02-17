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

## Deployment workflow 
This repo includes `.github/workflows/deploy-scaler.yml` to update the scaler
Function App when a service build completes.

### Inputs
- `function_app_name` (required)
- `resource_group` (required)
- `aci_image` (required)
- `app_settings_json` (optional JSON string of extra env vars)
- `restart_app` (optional, default true)
Note: app settings updates are additive. Only the keys provided are updated;
existing Function App settings are not cleared.
Secrets:
- `TDEI_CORE_AZURE_CREDS`

### Trigger from a service repo
Use `repository_dispatch` with event type `service-deploy`:
```json
{
  "event_type": "service-deploy",
  "client_payload": {
    "function_app_name": "my-scaler-func",
    "resource_group": "my-rg",
    "aci_image": "myregistry.azurecr.io/myservice:2025-02-10",
    "app_settings_json": "{\"INSTANCE_SUBSCRIPTION_ENV_NAME\":\"VALIDATION_REQ_SUB\",\"INSTANCE_FOO\":\"bar\"}",
    "restart_app": "true"
  }
}
```

### Multi-environment (single workflow)
The service workflow maps `dev`, `stage`, and `main` branches to their respective
Function App name + resource group (example in `docs/service-workflow-example.yml`).
ACR can remain shared.

## Deploy scaler code (manual)
`.github/workflows/deploy-scaler-code.yml` deploys this repo to the Function App
via manual `workflow_dispatch`. It uses the following secret:
- `TDEI_CORE_AZURE_CREDS`
Note: code deployment does not modify Function App settings.

## Service & Scalar Integration Flow Diagram

```mermaid
flowchart TD
  A[Service PR merged to dev] --> B[Service workflow: Build image]
  B --> C[Push image to ACR]
  C --> D[Trigger scaler repo via repository_dispatch]

  D --> E[Scaler workflow: deploy-scaler.yml]
  E --> F[Azure login (OIDC)]
  F --> G[Update Function App settings]
  G --> H[Set ACI_IMAGE + app_settings_json]
  H --> I[Restart Function App]
```
