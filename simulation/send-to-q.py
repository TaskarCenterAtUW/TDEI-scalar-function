#!/usr/bin/env python3
"""
Send messages to an Azure Service Bus request topic for each file path,
then wait for completion messages on a completion topic/subscription.
Writes a CSV report with file_upload_path, file size, message_id, success, message, etc.
"""

import argparse
import csv
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusSubQueue

load_dotenv()

SERVICE_BUS_CONNECTION_STR = os.getenv("SERVICE_BUS_CONNECTION")
REQUEST_TOPIC = os.getenv("REQUEST_TOPIC", "osw-validation-scalar-request")
COMPLETION_TOPIC = os.getenv("COMPLETION_TOPIC", "osw-validation-scalar-response")
COMPLETION_SUBSCRIPTION = os.getenv("COMPLETION_SUBSCRIPTION", "res-handler")
DEFAULT_USER_ID = os.getenv("USER_ID", "50a49bf7-fa92-46f8-a53f-f4b732945013")
DEFAULT_FILES_JSON = "files-list.json"  # in current directory when no file input given


@dataclass
class JobRecord:
    """One sent job and its completion result."""
    file_upload_path: str
    file_size_bytes: int
    message_id: str
    sent_at: datetime
    completed_at: datetime | None = None
    success: bool | None = None
    message: str | None = None
    error: str | None = None

    @property
    def file_size_mb(self) -> float:
        return round(self.file_size_bytes / (1024 * 1024), 2)

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at and self.sent_at:
            return (self.completed_at - self.sent_at).total_seconds()
        return None


def get_file_size(path: str) -> int:
    """Return size in bytes for a local file; 0 for URL or missing file."""
    if path.startswith("http://") or path.startswith("https://"):
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def send_message_to_topic(
    message_body: str | dict,
    topic_name: str,
    properties: dict | None = None,
) -> None:
    """Send one message to the given topic."""
    if not SERVICE_BUS_CONNECTION_STR or not topic_name:
        raise ValueError("SERVICE_BUS_CONNECTION and topic name must be set.")

    if isinstance(message_body, dict):
        body_str = json.dumps(message_body)
    else:
        body_str = str(message_body)

    with ServiceBusClient.from_connection_string(SERVICE_BUS_CONNECTION_STR) as client:
        with client.get_topic_sender(topic_name=topic_name) as sender:
            if properties:
                msg = ServiceBusMessage(body_str, application_properties=properties)
            else:
                msg = ServiceBusMessage(body_str)
            sender.send_messages(msg)


def run_receiver(
    completion_topic: str,
    completion_subscription: str,
    results: dict[str, JobRecord],
    stop_event: threading.Event,
    receive_timeout_seconds: int,
    report_path: str | None = None,
    report_lock=None,
) -> None:
    """
    Run in a thread: receive from completion subscription and update results
    by message_id. Expects body with messageId and data.success, data.message.
    When report_path and report_lock are set, writes CSV after each completion so context is not lost.
    """
    def flush_report() -> None:
        if report_path and report_lock:
            with report_lock:
                write_csv_report(list(results.values()), report_path)

    def parse_body(msg) -> dict:
        """Best-effort parser for Service Bus message body."""
        try:
            raw_parts = []
            for part in msg.body:
                if isinstance(part, (bytes, bytearray)):
                    raw_parts.append(bytes(part))
                else:
                    raw_parts.append(str(part).encode("utf-8"))
            body_text = b"".join(raw_parts).decode("utf-8")
            return json.loads(body_text)
        except Exception:
            try:
                return json.loads(str(msg))
            except Exception:
                return {}

    if not SERVICE_BUS_CONNECTION_STR:
        return
    try:
        with ServiceBusClient.from_connection_string(SERVICE_BUS_CONNECTION_STR) as client:
            with client.get_subscription_receiver(
                topic_name=completion_topic,
                subscription_name=completion_subscription,
            ) as receiver:
                while not stop_event.is_set():
                    received = receiver.receive_messages(
                        max_message_count=10,
                        max_wait_time=min(5, receive_timeout_seconds),
                    )
                    for msg in received:
                        message_id = None
                        try:
                            body = parse_body(msg)
                            message_id = _extract_message_id(body, msg)
                            data = body.get("data") or {}
                            if isinstance(data, str):
                                data = {}
                            success_raw = data.get("success")
                            success: bool | None
                            if isinstance(success_raw, bool):
                                success = success_raw
                            elif isinstance(success_raw, str):
                                if success_raw.lower() in ("true", "1", "yes"):
                                    success = True
                                elif success_raw.lower() in ("false", "0", "no"):
                                    success = False
                                else:
                                    success = None
                            else:
                                success = None
                            compl_message = data.get("message") or data.get("messageText") or ""

                            if message_id and message_id in results:
                                rec = results[message_id]
                                rec.completed_at = datetime.now()
                                rec.success = success
                                rec.message = compl_message
                                if success is False and compl_message:
                                    rec.error = compl_message
                                receiver.complete_message(msg)
                                flush_report()
                            else:
                                # Avoid repeated redelivery loops on unmatched messages.
                                receiver.complete_message(msg)
                        except Exception as e:
                            if message_id and message_id in results:
                                results[message_id].error = str(e)
                                results[message_id].completed_at = datetime.now()
                                flush_report()
                                receiver.complete_message(msg)
                            else:
                                receiver.abandon_message(msg)
    except Exception as e:
        for rec in results.values():
            if rec.completed_at is None and rec.error is None:
                rec.error = f"Receiver error: {e}"
        flush_report()
    finally:
        stop_event.set()


def _parse_servicebus_body(msg) -> dict:
    """Best-effort parser for Service Bus message body."""
    try:
        raw_parts = []
        for part in msg.body:
            if isinstance(part, (bytes, bytearray)):
                raw_parts.append(bytes(part))
            else:
                raw_parts.append(str(part).encode("utf-8"))
        body_text = b"".join(raw_parts).decode("utf-8")
        return json.loads(body_text)
    except Exception:
        try:
            return json.loads(str(msg))
        except Exception:
            return {}


def _extract_message_id(body: dict, msg) -> str | None:
    """Extract message id from body, application properties, or broker metadata."""
    mid = None
    if isinstance(body, dict):
        mid = body.get("messageId") or body.get("message_id") or body.get("id")
        if not mid:
            data = body.get("data")
            if isinstance(data, dict):
                mid = data.get("messageId") or data.get("message_id") or data.get("id")
    if not mid:
        props = getattr(msg, "application_properties", None) or {}
        if isinstance(props, dict):
            mid = (
                props.get("messageId")
                or props.get("message_id")
                or props.get("correlation_id")
                or props.get("job_id")
            )
    if not mid:
        mid = getattr(msg, "message_id", None)
    return str(mid).strip() if mid is not None else None


def mark_records_from_dlq(
    completion_topic: str,
    completion_subscription: str,
    records: dict[str, JobRecord],
    report_path: str,
    report_lock,
    max_peek: int = 1000,
) -> int:
    """
    Peek completion subscription DLQ and mark matching pending records as failed.
    Returns number of records newly marked from DLQ.
    """
    pending_ids = {
        mid for mid, rec in records.items()
        if rec.completed_at is None and not rec.error
    }
    if not pending_ids or not SERVICE_BUS_CONNECTION_STR:
        return 0

    found_ids: set[str] = set()
    next_seq = None
    scanned = 0

    with ServiceBusClient.from_connection_string(SERVICE_BUS_CONNECTION_STR) as client:
        with client.get_subscription_receiver(
            topic_name=completion_topic,
            subscription_name=completion_subscription,
            sub_queue=ServiceBusSubQueue.DEAD_LETTER,
        ) as receiver:
            while scanned < max_peek and pending_ids - found_ids:
                msgs = receiver.peek_messages(max_message_count=50, sequence_number=next_seq)
                if not msgs:
                    break
                for msg in msgs:
                    scanned += 1
                    body = _parse_servicebus_body(msg)
                    mid = _extract_message_id(body, msg)
                    if mid in pending_ids:
                        found_ids.add(mid)
                    seq = getattr(msg, "sequence_number", None)
                    if seq is not None:
                        next_seq = seq + 1
                    if scanned >= max_peek:
                        break

    if not found_ids:
        return 0

    now = datetime.now()
    for mid in found_ids:
        rec = records[mid]
        rec.completed_at = now
        rec.success = False
        rec.message = "Found in dead letter queue"
        rec.error = rec.error or "Found in dead letter queue"

    with report_lock:
        write_csv_report(list(records.values()), report_path)
    return len(found_ids)


def load_file_paths_from_json(json_path: str) -> list[tuple[str, int]]:
    """
    Load file list and sizes from a JSON file.
    Expects either:
      - An array: [ {"file_upload_path": "...", "file_size_mb": 10}, ... ]
      - An object with "files" key: { "files": [ ... ] }
    Each item may have: file_upload_path (or path, file_path) and optional file_size_mb or file_size_bytes.
    """
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {json_path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "files" in data:
        items = data["files"]
    else:
        raise ValueError("JSON must be an array or an object with a 'files' array")
    if not isinstance(items, list):
        raise ValueError("'files' must be an array")
    result: list[tuple[str, int]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        path_str = (
            item.get("file_upload_path")
            or item.get("path")
            or item.get("file_path")
        )
        if not path_str:
            continue
        path_str = str(path_str).strip()
        size = 0
        if "file_size_bytes" in item:
            try:
                size = int(item["file_size_bytes"])
            except (TypeError, ValueError):
                size = get_file_size(path_str)
        elif "file_size_mb" in item:
            try:
                size = int(float(item["file_size_mb"]) * 1024 * 1024)
            except (TypeError, ValueError):
                size = get_file_size(path_str)
        else:
            size = get_file_size(path_str)
        result.append((path_str, size))
    return result


def load_file_paths(
    files: list[str],
    files_from: str | None,
    files_json: str | None,
) -> list[tuple[str, int]]:
    """Return list of (file_upload_path, file_size_bytes). JSON takes precedence if provided."""
    if files_json:
        return load_file_paths_from_json(files_json)
    paths_with_size: list[tuple[str, int]] = []

    if files_from:
        path = Path(files_from)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {files_from}")
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",", 1)]
                path_str = parts[0]
                if len(parts) > 1 and parts[1]:
                    try:
                        size = int(float(parts[1]) * 1024 * 1024)
                    except ValueError:
                        size = get_file_size(path_str)
                else:
                    size = get_file_size(path_str)
                paths_with_size.append((path_str, size))

    for p in files or []:
        p = p.strip()
        if not p:
            continue
        size = get_file_size(p)
        paths_with_size.append((p, size))

    return paths_with_size


def load_records_from_csv(csv_path: str) -> dict[str, JobRecord]:
    """
    Load job records from an existing report CSV (e.g. from a previous run that stopped).
    Rows with completed_at already set are left as-is so they count as done; others will be
    updated when completions are received. Returns dict keyed by message_id.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Report file not found: {csv_path}")
    records: dict[str, JobRecord] = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "message_id" not in reader.fieldnames:
            raise ValueError("CSV must have a message_id column")
        for row in reader:
            message_id = (row.get("message_id") or "").strip()
            if not message_id:
                continue
            file_upload_path = (row.get("file_upload_path") or "").strip()
            try:
                file_size_bytes = int(row.get("file_size_bytes") or 0)
            except (TypeError, ValueError):
                file_size_bytes = 0
            sent_at_str = (row.get("sent_at") or "").strip()
            try:
                sent_at = datetime.strptime(sent_at_str, "%Y-%m-%d %H:%M:%S") if sent_at_str else datetime.now()
            except ValueError:
                sent_at = datetime.now()
            completed_at = None
            completed_at_str = (row.get("completed_at") or "").strip()
            if completed_at_str:
                try:
                    completed_at = datetime.strptime(completed_at_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            success_raw = (row.get("success") or "").strip()
            success: bool | None = None
            if success_raw:
                if success_raw.lower() in ("true", "1", "yes"):
                    success = True
                elif success_raw.lower() in ("false", "0", "no"):
                    success = False
            message = (row.get("message") or "").strip()
            error = (row.get("error") or "").strip()
            rec = JobRecord(
                file_upload_path=file_upload_path,
                file_size_bytes=file_size_bytes,
                message_id=message_id,
                sent_at=sent_at,
                completed_at=completed_at,
                success=success,
                message=message or None,
                error=error or None,
            )
            records[message_id] = rec
    return records


def write_csv_report(records: list[JobRecord], output_path: str) -> str:
    """Write CSV with file_upload_path, file_size, message_id, sent_at, completed_at, success, message, etc.
    Flushes and syncs to disk so no updates are lost."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "file_upload_path", "file_size_bytes", "file_size_mb", "message_id",
        "sent_at", "completed_at", "duration_seconds", "success", "message", "error",
    ]
    # Stable order by sent_at so rows are consistent
    sorted_records = sorted(records, key=lambda r: (r.sent_at, r.message_id))
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in sorted_records:
            w.writerow({
                "file_upload_path": r.file_upload_path,
                "file_size_bytes": r.file_size_bytes,
                "file_size_mb": r.file_size_mb,
                "message_id": r.message_id,
                "sent_at": r.sent_at.strftime("%Y-%m-%d %H:%M:%S"),
                "completed_at": r.completed_at.strftime("%Y-%m-%d %H:%M:%S") if r.completed_at else "",
                "duration_seconds": r.duration_seconds if r.duration_seconds is not None else "",
                "success": r.success if r.success is not None else "",
                "message": (r.message or "")[:500],
                "error": r.error or "",
            })
        f.flush()
        os.fsync(f.fileno())
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send validation request messages per file path, wait for completion, write CSV report.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="File upload paths (local paths or URLs).",
    )
    parser.add_argument(
        "--files-from",
        metavar="FILE",
        help="Read paths from text file (one per line; optional second column: size_mb).",
    )
    parser.add_argument(
        "--files-json",
        metavar="FILE",
        default=None,
        help=f"JSON file for file list and sizes (default: {DEFAULT_FILES_JSON} in current dir if no other input).",
    )
    parser.add_argument(
        "--num-requests",
        "-n",
        type=int,
        default=None,
        metavar="N",
        help="Total number of requests to send. If greater than number of files, files are repeated in order (default: one per file).",
    )
    parser.add_argument(
        "--request-topic",
        default=REQUEST_TOPIC,
        help=f"Request topic (default: env REQUEST_TOPIC or {REQUEST_TOPIC}).",
    )
    parser.add_argument(
        "--completion-topic",
        default=COMPLETION_TOPIC,
        help="Topic for completion messages.",
    )
    parser.add_argument(
        "--completion-subscription",
        default=COMPLETION_SUBSCRIPTION,
        help="Subscription name for completion topic.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help="user_id in payload data.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=300000,
        help="Max seconds to wait for all completions (default: 600).",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help="Output CSV report path (default: reports/q_msg_report_<timestamp>.csv).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list files and payloads, do not send or receive.",
    )
    parser.add_argument(
        "--resume",
        metavar="REPORT",
        default=None,
        help="Resume/track: load job list from an existing report CSV and only listen on the completion queue to update it (no new messages sent).",
    )
    args = parser.parse_args()

    # Resume mode: load records from report and only track completions
    if args.resume:
        if not SERVICE_BUS_CONNECTION_STR:
            print("SERVICE_BUS_CONNECTION environment variable is not set.")
            return
        try:
            records = load_records_from_csv(args.resume)
        except (FileNotFoundError, ValueError) as e:
            parser.error(str(e))
        if not records:
            parser.error(f"No job records found in {args.resume}")
        report_path = args.resume
        report_lock = threading.Lock()
        pending = sum(1 for r in records.values() if r.completed_at is None and not r.error)
        print(f"Resume: loaded {len(records)} job(s) from {report_path} ({pending} pending). Tracking completion queue...")
        stop_event = threading.Event()
        receiver_thread = threading.Thread(
            target=run_receiver,
            args=(
                args.completion_topic,
                args.completion_subscription,
                records,
                stop_event,
                args.wait_timeout,
                report_path,
                report_lock,
            ),
            daemon=True,
        )
        receiver_thread.start()
        start = time.time()
        last_dlq_check = 0.0
        while time.time() - start < args.wait_timeout:
            if time.time() - last_dlq_check >= 5:
                try:
                    dlq_marked = mark_records_from_dlq(
                        args.completion_topic,
                        args.completion_subscription,
                        records,
                        report_path,
                        report_lock,
                    )
                    if dlq_marked:
                        print(f"Marked {dlq_marked} pending message(s) as failed from DLQ")
                except Exception as dlq_err:
                    print(f"DLQ check warning: {dlq_err}")
                last_dlq_check = time.time()
            done = sum(1 for r in records.values() if r.completed_at is not None or (r.error is not None and r.error != ""))
            if done >= len(records):
                break
            time.sleep(2)
        stop_event.set()
        receiver_thread.join(timeout=10)
        with report_lock:
            write_csv_report(list(records.values()), report_path)
        success_count = sum(1 for r in records.values() if r.success is True)
        fail_count = sum(1 for r in records.values() if r.success is False or (r.error and r.error.strip()))
        print(f"Done. Success: {success_count}, Failed/Error: {fail_count}. Report: {report_path}")
        return

    files_json = args.files_json
    if not args.files and not args.files_from:
        if files_json is None and Path(DEFAULT_FILES_JSON).exists():
            files_json = DEFAULT_FILES_JSON
            print(f"Using file list from {DEFAULT_FILES_JSON}")
        if files_json is None:
            parser.error(f"Provide file path(s), --files-from FILE, or ensure {DEFAULT_FILES_JSON} exists in current directory")

    paths_with_size = load_file_paths(args.files, args.files_from, files_json)
    if not paths_with_size:
        print("No file paths to process.")
        return

    num_requests = args.num_requests
    if num_requests is not None:
        if num_requests < 1:
            parser.error("--num-requests must be >= 1")
        n = len(paths_with_size)
        paths_with_size = [paths_with_size[i % n] for i in range(num_requests)]
        print(f"Total requests: {num_requests} (cycling over {n} file(s))")

    if not SERVICE_BUS_CONNECTION_STR:
        print("SERVICE_BUS_CONNECTION environment variable is not set.")
        return

    # Build job records and payloads
    records: dict[str, JobRecord] = {}
    for file_upload_path, file_size_bytes in paths_with_size:
        message_id = f"e2e-{uuid.uuid4()}"
        rec = JobRecord(
            file_upload_path=file_upload_path,
            file_size_bytes=file_size_bytes,
            message_id=message_id,
            sent_at=datetime.now(),
        )
        records[message_id] = rec

    if args.dry_run:
        for mid, rec in records.items():
            print(f"  {rec.file_upload_path} ({rec.file_size_mb} MB) -> message_id={mid}")
        print(f"Would send {len(records)} message(s) and wait for completions.")
        return

    report_path = args.report or f"reports/q_msg_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    report_lock = threading.Lock()

    # Start receiver thread (writes CSV on each completion so we don't lose context)
    stop_event = threading.Event()
    receiver_thread = threading.Thread(
        target=run_receiver,
        args=(
            args.completion_topic,
            args.completion_subscription,
            records,
            stop_event,
            args.wait_timeout,
            report_path,
            report_lock,
        ),
        daemon=True,
    )
    receiver_thread.start()

    # Send all messages
    print(f"Sending {len(records)} message(s) to topic '{args.request_topic}'...")
    for message_id, rec in records.items():
        payload = {
            "messageId": message_id,
            "messageType": "osw_validation_only|osw_validation_only",
            "publishedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "message": "",
            "data": {
                "user_id": args.user_id,
                "file_upload_path": rec.file_upload_path,
            },
        }
        props = {"file_size_mb": rec.file_size_mb}
        if rec.file_size_bytes:
            props["file_size_bytes"] = rec.file_size_bytes
        try:
            send_message_to_topic(payload, topic_name=args.request_topic, properties=props)
            print(f"  Sent message_id={message_id} path={rec.file_upload_path}")
        except Exception as e:
            rec.error = str(e)
            rec.success = False
            rec.completed_at = datetime.now()
            print(f"  Failed to send {message_id}: {e}")

    # Write report immediately with request details (completion columns empty until we get responses)
    with report_lock:
        write_csv_report(list(records.values()), report_path)
    print(f"Report (live): {report_path}")

    # Wait for all completions or timeout; report is updated on each completion
    start = time.time()
    last_dlq_check = 0.0
    while time.time() - start < args.wait_timeout:
        # Periodically inspect DLQ to avoid losing context when completion messages dead-letter.
        if time.time() - last_dlq_check >= 5:
            try:
                dlq_marked = mark_records_from_dlq(
                    args.completion_topic,
                    args.completion_subscription,
                    records,
                    report_path,
                    report_lock,
                )
                if dlq_marked:
                    print(f"Marked {dlq_marked} pending message(s) as failed from DLQ")
            except Exception as dlq_err:
                # Keep completion tracking alive even if DLQ peek has intermittent errors.
                print(f"DLQ check warning: {dlq_err}")
            last_dlq_check = time.time()
        done = sum(1 for r in records.values() if r.completed_at is not None or r.error is not None)
        if done >= len(records):
            break
        time.sleep(2)
    stop_event.set()
    # Give receiver time to finish its last flush_report() after processing final message(s)
    receiver_thread.join(timeout=10)

    # Final report write after receiver has stopped, so we persist the full state
    with report_lock:
        write_csv_report(list(records.values()), report_path)
    success_count = sum(1 for r in records.values() if r.success is True)
    fail_count = sum(1 for r in records.values() if r.success is False or r.error)
    print(f"Done. Success: {success_count}, Failed/Error: {fail_count}. Report: {report_path}")


if __name__ == "__main__":
    main()
