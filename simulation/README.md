# send-to-q

Send validation request messages to an Azure Service Bus topic for each file path, wait for completion messages on a response subscription, and write a CSV report with results.

## Requirements

- Python 3.10+
- Dependencies: `azure-servicebus`, `python-dotenv`

```bash
pip install azure-servicebus python-dotenv
```

## Environment

Create a `.env` file (or export variables) with:

| Variable | Description |
|----------|-------------|
| `SERVICE_BUS_CONNECTION` | **Required.** Azure Service Bus connection string. |
| `REQUEST_TOPIC` | Request topic name (default: `osw-validation-scalar-request`). |
| `COMPLETION_TOPIC` | Topic for completion/response messages (default: `osw-validation-scalar-response`). |
| `COMPLETION_SUBSCRIPTION` | Subscription name on completion topic (default: `res-handler`). |
| `USER_ID` | User ID used in payload (default: `50a49bf7-fa92-46f8-a53f-f4b732945013`). |

## Usage

### Basic: use default file list

If `files-list.json` exists in the current directory and you pass no file arguments, that file is used automatically:

```bash
python send-to-q.py
```

### File inputs

**From a JSON file:**

```bash
python send-to-q.py --files-json my-files.json
```

**JSON format** — either an array or an object with a `files` array. Each item can have:

- `file_upload_path` (or `path`, `file_path`) — required
- `file_size_mb` or `file_size_bytes` — optional (local file size is used if omitted)

Example `files-list.json`:

```json
[
  {"file_upload_path": "https://storage.example.com/data.zip", "file_size_mb": 200},
  {"file_upload_path": "/local/path/file.osw", "file_size_bytes": 1024000}
]
```

Or with a wrapper:

```json
{
  "files": [
    {"file_upload_path": "https://example.com/file.zip"}
  ]
}
```

### Sending multiple requests (repeat file list)

Send 20 requests, cycling over the files in your list:

```bash
python send-to-q.py --files-json files-list.json --num-requests 20
```

### Overriding topics and subscription

```bash
python send-to-q.py --request-topic my-request-topic \
  --completion-topic my-response-topic \
  --completion-subscription my-sub \
  --files-json files-list.json
```

### Report output

**Default:** report is written to `reports/q_msg_report_YYYYMMDD_HHMMSS.csv`.

**Custom path:**

```bash
python send-to-q.py --files-json files-list.json --report reports/my_run.csv
```

The script updates the CSV as each completion is received (and flushes to disk), so partial results are preserved if the process is interrupted.

### Timeout

Maximum time (in seconds) to wait for all completions. Default is `300000`.

```bash
python send-to-q.py --wait-timeout 600 --files-json files-list.json
```

### Dry run

Print which files and message IDs would be sent without sending or receiving:

```bash
python send-to-q.py --files-json files-list.json --dry-run
```

### Resume / track existing report

If the script stopped (crash, Ctrl+C, etc.) after sending messages but before all completions were received, you can resume by pointing it at the existing report. The script will **not** send any new messages; it only listens on the completion queue (and checks the DLQ) and updates the report as responses arrive.

```bash
python send-to-q.py --resume reports/q_msg_report_20260224_100916.csv
```

You can override completion topic/subscription and timeout:

```bash
python send-to-q.py --resume reports/my_run.csv --completion-topic my-response-topic --wait-timeout 3600
```

The report file is updated in place as completions are received.

## Options summary

| Option | Short | Description |
|--------|--------|-------------|
| `files` | — | File paths (positional); local paths or URLs. |
| `--files-from` | — | Text file: one path per line; optional second column = size_mb. |
| `--files-json` | — | JSON file for file list (default: `files-list.json` if no other input). |
| `--num-requests` | `-n` | Total requests; if &gt; number of files, files are repeated in order. |
| `--request-topic` | — | Request topic name. |
| `--completion-topic` | — | Completion/response topic. |
| `--completion-subscription` | — | Subscription on completion topic. |
| `--user-id` | — | `user_id` in payload. |
| `--wait-timeout` | — | Max seconds to wait for completions (default: 300000). |
| `--report` | — | Output CSV path (default: `reports/q_msg_report_<timestamp>.csv`). |
| `--dry-run` | — | List files and payloads only; do not send or receive. |
| `--resume` | — | Load jobs from an existing report CSV and only track completions (no new sends). |

## Report CSV columns

| Column | Description |
|--------|-------------|
| `file_upload_path` | Path or URL sent in the request. |
| `file_size_bytes` | Size in bytes. |
| `file_size_mb` | Size in MB. |
| `message_id` | ID of the sent message (used to match completions). |
| `sent_at` | When the request was sent. |
| `completed_at` | When a completion was received (empty if none). |
| `duration_seconds` | Time from send to completion. |
| `success` | `True`/`False` from completion payload. |
| `message` | Completion message text. |
| `error` | Error or “Found in dead letter queue” if completion was in DLQ. |

## Dead letter queue (DLQ)

The script periodically checks the completion subscription’s dead letter queue. If a completion message is found there and its `message_id` matches a pending job, that job is marked as failed with `success=False` and `error` set to `"Found in dead letter queue"`, and the report is updated. This prevents runs from hanging when completions are dead-lettered instead of delivered to the main subscription.

## Example workflow

```bash
# 1. Ensure .env has SERVICE_BUS_CONNECTION (and optional topic/subscription overrides)
# 2. Create files-list.json with your file paths
# 3. Run (report goes to reports/q_msg_report_<timestamp>.csv)
python send-to-q.py
