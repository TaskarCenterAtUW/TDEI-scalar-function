# ACI Scale Management - Design Summary

## Purpose
Timer-triggered Azure Function that peeks Service Bus topic subscriptions and provisions Azure Container Instances (ACI) to process validation requests. Each container is sized by input file size.

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
- `SUBSCRIPTION_ENV_NAME` is not passed through to the container, as it just holds the service env variable name for which service should listen.

Service Bus
- `SB_CONNECTION_STR`
- `SB_NAMESPACE` (optional if it can be derived from connection string)
- `SB_TOPIC_NAME`

### Service Bus message (required fields for scaler)
Only two fields are required; other properties are ignored and may vary by service.

- `messageId` from Service Bus metadata
- `file_size_mb` from message body (top-level or `data.file_size_mb`)
```json
{
        "messageId": "5e1a464d-9d69-4e74-871b-474bdc31da20",
        "messageType": "osw_validation_only|osw_validation_only",
        "publishedDate": "2025-03-20T13:18:42.501Z",
        "message": "",
        "data": {
            "file_size_mb": 50
        }
    }
```

## How messages are read
- The function peeks messages from each subscription on the configured topic.
- Peek does not settle messages; it is read-only and used only to decide provisioning.
- Scalar loops thorugh topic subscriptions are processed in sorted name order to keep deterministic behavior.
- Pass 1 provisions at most one message per subscription.
- Pass 2 fills remaining capacity with more messages, still in order.
- Scalar triggers every minute and same process repeats.

## Provisioning flow
1. List container groups tagged with `managed_by = ACI_NAME_PREFIX`.
2. Split list into active vs terminal states.
3. Compute remaining capacity: `ACI_MAX_INSTANCES - active_count`.
4. Skip if `message_id` already exists in active or terminal container tags.
5. Skip if `delivery_count` is at or above subscription `max_delivery_count`.
6. Calculate memory: `(file_size_mb / 1024) * ACI_MEMORY_MULTIPLIER`, clamped by min/max.
7. Create an ACI container group with tags for reference:
   - `managed_by`
   - `message_id`
   - `file_size_mb`
8. Delete terminal containers after provisioning.

## Validations
- Rejects messages if JSON cannot be parsed.
- Rejects messages if `message_id` is missing.
- Rejects messages if `file_size_mb` is missing or not numeric.

## Duplicate handling
- Uses existing active + terminal containers' `message_id` tags to avoid provisioning duplicates.
- Duplicate messages within the same peek batch are skipped after the first provision.
- With peek-only, messages remain in the subscription unless the container service settles them.

## Race conditions / parallelism
- Topic messages are filtered and messages are available in right subscription, when we provision we set the container
  instance to pick messages from same subscription. This takes care of race condition. 

## Crash / failure scenarios
- **Function crash after provisioning**: the container still exists and is tracked via tags.
- **Provisioning failure**: logged and does not stop other subscriptions from being processed.
- **Deleting containers**: cleaned up on each run.
- **Service Bus errors**: logged; the run completes without provisioning.

## Notes
- The scaler does not process or settle Service Bus messages.
- The scaler does not validate or use any fields other than `message_id` and `file_size_mb`.
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

