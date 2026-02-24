# ACI Scale Management - Design Summary

## Purpose
Timer-triggered Azure Function that peeks Service Bus topic subscriptions and
provisions Azure Container Instances (ACI) to process validation requests. Each
container is sized by input file size from Service Bus application properties.

## Inputs
### Environment variables
Azure
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_RESOURCE_GROUP`

ACI
- `ACI_IMAGE`
- `ACI_NAME_PREFIX`
- `ACI_LOCATION`
- `ACI_MAX_INSTANCES`
- `ACI_DEFAULT_CPU`
- `ACI_MEMORY_MULTIPLIER`
- `ACI_MIN_MEMORY_GB`
- `ACI_MAX_MEMORY_GB`
- `ACR_SERVER`
- `ACR_USERNAME`
- `ACR_PASSWORD`

Container
- `INSTANCE_*`
- `INSTANCE_SUBSCRIPTION_ENV_NAME` (optional) sets which container env key receives the subscription name; this is required for service to know which subscription to read messages from.
- The scaler strips the `INSTANCE_` prefix and passes remaining keys to the container.
- `SUBSCRIPTION_ENV_NAME` (from `INSTANCE_SUBSCRIPTION_ENV_NAME`) is not passed to the container; it only names which env key receives the subscription name (e.g. `VALIDATION_REQ_SUB`).

Service Bus
- `SB_CONNECTION_STR`
- `SB_NAMESPACE` (optional if it can be derived from connection string)
- `SB_TOPIC_NAME`
- `SKIP_SUBSCRIPTIONS` (optional comma-separated list of subscription names to skip)
- `PROVISIONING_BATCH_SIZE` (optional max messages to provision per invocation, default 10; avoids timeout)
- `PROVISIONING_MAX_WORKERS` (optional max parallel workers, default 4)
- `PROVISIONING_PEEK_MAX` (optional max peek batch size per subscription, default 50)
- `PROVISIONING_CONFIRM_MESSAGE` (optional confirm message still present before provisioning, default true)
- `PROVISIONING_IN_FLIGHT_MESSAGE_CHECK` (optional check message presence while ACI provisioning is in progress, default true)
- `PROVISIONING_IN_FLIGHT_PEEK_MAX` (optional peek size for in-flight presence checks, default 100)
- `PROVISIONING_IN_FLIGHT_CHECK_INTERVAL_SECONDS` (optional seconds between in-flight checks, default 1)
- `PROVISIONING_POST_CREATE_PEEK_MAX` (optional peek size for post-provision presence check, default 100)
- `PROVISIONING_POST_CREATE_CONFIRM_CHECKS` (optional absence checks after provisioning before deleting, default 1)
- `PROVISIONING_POST_CREATE_CONFIRM_INTERVAL_SECONDS` (optional seconds between post-create checks, default 0)
- `PROVISIONING_ORPHAN_EMPTY_SUB_PEEK_MAX` (optional peek size for running-orphan check when subscription is empty, default 1)

### Service Bus message (required fields for scaler)
Only two fields are required; other properties are ignored and may vary by service.

- `messageId` from Service Bus metadata (`message_id` in SDK object)
- `file_size_mb` from Service Bus application properties
```json
{
        "messageId": "5e1a464d-9d69-4e74-871b-474bdc31da20",
        "messageType": "osw_validation_only|osw_validation_only",
        "publishedDate": "2025-03-20T13:18:42.501Z",
        "message": "",
        "data": {}
    }
```

## How messages are read
- The function peeks messages from each subscription on the configured topic.
- Peek does not settle messages; it is read-only and used only to decide provisioning.
- Scaler loops through topic subscriptions in sorted name order for deterministic behavior.
- Provisioning uses a thread pool (bounded by `PROVISIONING_MAX_WORKERS`) to run subscriptions in parallel.
- Subscriptions listed in `SKIP_SUBSCRIPTIONS` are filtered out before processing.
- Each subscription peek can look ahead up to `PROVISIONING_PEEK_MAX` to skip
  duplicates and reach the next unprocessed message.
- When `PROVISIONING_CONFIRM_MESSAGE=true`, the scaler confirms the message is still present before provisioning by peeking again and matching `message_id`.
- During ACI provisioning wait, the scaler can re-check message presence (`PROVISIONING_IN_FLIGHT_MESSAGE_CHECK`); if absent, it deletes the in-progress container group and stops waiting.
- After ACI provisioning reports success, the scaler re-checks message presence and deletes newly provisioned containers if message is absent.
- Pass 1 provisions at most one message per subscription.
- Pass 2 fills remaining capacity with more messages, still in order.
- Scaler triggers every minute and the same process repeats.

## Provisioning flow
1. List container groups tagged with `managed_by = ACI_NAME_PREFIX`.
2. Split list into active vs terminal states (terminal = container instance state `Failed` or `Terminated`).
3. Compute remaining capacity: `ACI_MAX_INSTANCES - active_count`; cap at `PROVISIONING_BATCH_SIZE` (default 10) so each invocation provisions at most that many messages (avoids function timeout).
4. Build existing keys from active groups: `(subscription_name, message_id)` (both normalized to string).
5. Skip provisioning if `(subscription_name, message_id)` already exists in active container tags.
6. Calculate memory: `(file_size_mb / 1024) * ACI_MEMORY_MULTIPLIER`, clamped by min/max,
   then rounded up to the nearest 0.1 GB (ACI requirement).
7. Create an ACI container group with tags for reference:
   - `managed_by`
   - `message_id`
   - `file_size_mb`
   - `subscription_name`
8. While waiting for provision completion, optionally poll Service Bus for message presence; if missing, delete the just-created container and abort this provision result.
9. After successful provisioning, verify message is still present; if missing, delete the new container.
10. Delete a container when **both** container instance state and provisioning state are terminal (e.g. container `Failed`/`Terminated` and provisioning `Succeeded`/`Failed`/`Terminated`). Capture last 20 lines of container logs before deletion.
11. Running orphan cleanup: if a running container has `message_id` + `subscription_name` tags and the tagged subscription is empty, delete that running container.

## Validations
- Rejects messages if JSON cannot be parsed.
- Rejects messages if `message_id` is missing.
- Rejects messages if `file_size_mb` is missing or not numeric in application properties.

## Duplicate handling
- Duplicate key is `(subscription_name, message_id)`; both are normalized to string so tags (which are strings) and in-run keys match.
- Uses existing active containers' tags to build this set and skip provisioning when the key already exists.
- Duplicate messages within the same peek batch are skipped after the first provision.
- With peek-only, messages remain in the subscription unless the container service settles them.

## Race conditions / parallelism
- Topic messages are filtered and messages are available in right subscription, when we provision we set the container
  instance to pick messages from same subscription. Ensuring container service picks right message for which it is provisioned. 

## Function timeout
- Timer function timeout is set to 10 minutes in `host.json` (Consumption plan maximum). If a run exceeds this, the host cancels the invocation.
- For longer-running workloads, use a Premium or Dedicated App Service plan (no runtime timeout), or reduce work per run (e.g. fewer subscriptions or instances).

## Crash / failure scenarios
- **Function crash after provisioning**: the container still exists and is tracked via tags.
- **Provisioning failure**: logged and does not stop other subscriptions from being processed.
- **Function timeout**: run is cancelled after the configured timeout; next timer run will retry.
- **Deleting containers**: cleaned up on each run.
- **Service Bus errors**: logged; the run completes without provisioning.

## Notes
- The scaler does not process or settle Service Bus messages.
- The scaler does not validate or use any fields other than `message_id` and `file_size_mb`.
- `file_size_mb` is read only from Service Bus application properties.
- Container service is expected to exit after processing and settling the message.
- `INSTANCE_SUBSCRIPTION_ENV_NAME` sets the env key for subscription name.
 
## Permissions and access required for Scalar function

### 1. Enable Managed Identity on Azure Function

**Steps**
- Azure Portal → **Function App**
- Navigate to **Identity**
- Under **System assigned**
   - Status: **On**
- Click **Save**

> This creates a service principal for the Function App.

### 2. Assign ACI Contributor

> Allow Azure Function to create, start, stop, and delete **ACI resource**.
**Steps**
- Azure Portal → **Resource Groups**
- Open the **target Resource Group**
- Click **Access control (IAM)**
- Click **+ Add** → **Add role assignment**
- Select:
   - Role: **Azure Container Instances Contributor**
   - Assign access to: **Managed identity**
   - Members: **Function App Name**
- Click **Review + assign**

### 3. Image pull (ACR)

**Steps**
- Azure Portal → **Azure Container Registry**
- Click **Access control (IAM)**
- Click **+ Add** → **Add role assignment**
- Select:
   - Role: **AcrPull**
   - Assign access to: **Managed identity**
   - Members: **Function App Name**
- Click **Review + assign**


### 4. Service Bus

**Steps**

- Navigate to the **Service Bus Namespace** in the Azure Portal.
- Select **Access control (IAM)**.
- Click **+ Add** → **Add role assignment**.
- Choose the appropriate role  **Reader**.
- Under **Assign access to**, select **Managed identity**.
- Click **+ Select members**, choose your **Azure Function App**.
- Click **Review + assign**.

