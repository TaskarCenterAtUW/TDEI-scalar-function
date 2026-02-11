import os


def pytest_addoption(parser):
    group = parser.getgroup("e2e")
    group.addoption(
        "--e2e-message-template",
        action="store",
        default=os.environ.get(
            "TEST_MESSAGE_JSON_PATH",
            "tests/data/integration_message.json",
        ),
        help="Path to JSON template for E2E message payload.",
    )
    group.addoption(
        "--e2e-file-sizes",
        action="store",
        default=os.environ.get("E2E_FILE_SIZES", ""),
        help="Comma-separated list of file_size_mb values.",
    )
    group.addoption(
        "--e2e-expected-subscriptions",
        action="store",
        default=os.environ.get("E2E_EXPECTED_SUBSCRIPTIONS", ""),
        help="Comma-separated list of expected subscription names.",
    )
    group.addoption(
        "--e2e-timeout-seconds",
        action="store",
        type=float,
        default=float(os.environ.get("E2E_TIMEOUT_SECONDS", "600")),
        help="Global timeout in seconds for E2E test waits.",
    )
    group.addoption(
        "--e2e-poll-interval-seconds",
        action="store",
        type=float,
        default=float(os.environ.get("E2E_POLL_INTERVAL_SECONDS", "10")),
        help="Polling interval in seconds for E2E test waits.",
    )
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
