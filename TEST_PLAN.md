## Test Plan

### Purpose
Validate the ACI scale manager end-to-end for production readiness, covering
correct provisioning, safety controls, reliability, and operational behavior.

### Scope
- Message ingestion via Service Bus topic subscriptions (peek-only).
- ACI provisioning sizing and tagging.
- Subscription filtering and parallel provisioning controls.
- Cleanup of terminal containers.
- Observability and failure handling.

Out of scope:
- Container workload logic and message settlement by the container service.
- Azure infra provisioning (assumed pre-existing).

### Environments
- Local dev environment using `.env` with real Azure resources.
- Optional staging Function App pointing at staging Service Bus/ACI resources.

### Pre-requisites
- Azure Function managed identity configured.
- RBAC: ACI Contributor, ACR Pull, Service Bus Reader.
- Service Bus topic with subscriptions and filters configured.
- ACI resource group and image available.
- Test messages prepared with `file_size_mb` in application properties.

### Test Data
- File sizes: 1MB, 50MB, 180MB, 500MB, 1024MB, 5GB.
- Subscriptions: at least 3 with non-overlapping filters.
- Messages with:
  - valid `file_size_mb` application property
  - missing `file_size_mb`
  - non-numeric `file_size_mb`
- Messages with unique `message_id` and payload `messageId` alignment.

### Functional Tests
1. **Single message provisioning**
   - Send one message with `file_size_mb` application property.
   - Verify: one container group created, tags include `message_id`, `file_size_mb`.
   - Verify memory matches `memory_multiplier`, clamped + rounded to 0.1GB.

2. **Multi-subscription routing**
   - Send messages that map to different subscriptions.
   - Verify: each message provisioned from correct subscription.

3. **Parallel provisioning**
   - Configure `PROVISIONING_MAX_WORKERS` > 1.
   - Send messages to all subscriptions at once.
   - Verify: multiple ACI groups created concurrently and within capacity.

4. **Capacity limiting**
   - Set `ACI_MAX_INSTANCES` small (e.g., 2).
   - Send >2 messages.
   - Verify: no more than 2 active containers at a time.

5. **Skip subscriptions**
   - Set `SKIP_SUBSCRIPTIONS` to include one active subscription.
   - Verify: messages on skipped subscriptions do not provision containers.

6. **Duplicate message handling**
   - Send duplicate `message_id` messages.
   - Verify: only one container is provisioned for that `message_id`.

7. **Terminal cleanup**
   - Allow containers to complete.
   - Run scaler; verify terminal containers are deleted.
   - Ensure running containers are not deleted.

8. **Log sanity**
   - Ensure provisioning logs show correct status transitions.
   - Confirm no false “failed” logs for running containers.

### Negative / Edge Tests
1. **Missing `file_size_mb`**
   - Send a message without application property.
   - Verify: scaler logs a validation warning and skips provisioning.

2. **Invalid `file_size_mb`**
   - Send `file_size_mb="big"`.
   - Verify: scaler logs a validation warning and skips.

3. **Service Bus outage**
   - Temporarily block or invalidate connection string.
   - Verify: scaler logs error and exits without crashing.

4. **ACI provisioning error**
   - Use an invalid image or quota limit.
   - Verify: error logged, other subscriptions continue.

### Performance / Load
- Burst test with N=50–200 messages across subscriptions.
- Verify: throughput improves with `PROVISIONING_MAX_WORKERS`.
- Verify: no memory sizing errors (0.1GB increments).

### Resilience / Recovery
- Restart Function App during active provisioning.
- Verify: existing containers are detected and not duplicated.
- Verify: cleanup works on next run.

### Security / Permissions
- Remove ACR Pull or ACI Contributor temporarily.
- Verify: failure is logged and system does not crash.
- Restore permissions after test.

### Acceptance Criteria
- 100% of valid messages provision correctly within timeout.
- No message causes more than one container instance.
- No terminal containers remain after cleanup run.
- No running containers are deleted.
- Errors are logged with actionable context.

### Test Execution Checklist
- [ ] Configure `.env` for test environment.
- [ ] Validate RBAC and ACR access.
- [ ] Run unit tests: `pytest -m "not integration" -vv`
- [ ] Run integration tests with multiple file sizes/subscriptions.
- [ ] Review logs and Azure portal resources.
- [ ] Confirm no leaked resources.

### Exit / Go-Live Criteria
- All functional and negative tests pass.
- No critical errors in logs.
- Performance acceptable for expected peak load.
- Monitoring and alerting in place for failures.
