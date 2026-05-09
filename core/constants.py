"""Shared defaults for runtime/config wiring."""

from __future__ import annotations

from typing import Any, Mapping


DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_DOTENV_PATH = ".env"

DEFAULT_LOGGER_NAME = "zongziledger"
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILE_PREFIX = "capture_"

DEFAULT_START_MARKER = "记账"
DEFAULT_PREFIX = f"#{DEFAULT_START_MARKER}"
DEFAULT_END_MARKER = "结束"

DEFAULT_ORDER_ID_DIGITS = 8
DEFAULT_ORDER_ID_REQUIRE_HASH = True

DEFAULT_CAPTURE_MODE = "auto"
DEFAULT_CAPTURE_BACKEND = "win32_memory"
DEFAULT_MAX_MESSAGES = 30
DEFAULT_CAPTURE_SCOPE = "today_new"
DEFAULT_CAPTURE_STATE_DIR = "data"

DEFAULT_DATA_PATH = "data/ledger.csv"
DEFAULT_JSON_OUTPUT_PATH = "data/ledger_details.json"
DEFAULT_MESSAGE_HASH_STATE_PATH = "data/message_hash_state.json"

DEFAULT_MANUAL_END_TOKEN = "END"
DEFAULT_MANUAL_ORDER_ID_SAMPLE = "26050101"

DEFAULT_DIFY_API_HOST = "127.0.0.1"
DEFAULT_DIFY_API_PORT = 8787
DEFAULT_DIFY_INGEST_PATH = "/api/dify/ingest"
DEFAULT_DIFY_INGEST_URL = ""
DEFAULT_DIFY_INGEST_TIMEOUT_SECONDS = 30
DEFAULT_DIFY_SOURCE_ID = "dify"
DEFAULT_DIFY_APPLY_CAPTURE_SCOPE = False
DEFAULT_DIFY_USE_DAILY_SETTLEMENT = False
DEFAULT_DIFY_USE_HASH_TIME_WINDOW = True
DEFAULT_DIFY_AUDIT_ENABLED = True
DEFAULT_DIFY_AUDIT_PATH = "data/dify_ingest_audit.jsonl"
DEFAULT_DIFY_RESPONSE_PREVIEW_LIMIT = 20

DEFAULT_DIFY_REMOTE_ENABLED = False
DEFAULT_DIFY_REMOTE_PRIORITY = True
DEFAULT_DIFY_REMOTE_FALLBACK_LOCAL = True
DEFAULT_DIFY_REMOTE_API_URL = ""
DEFAULT_DIFY_REMOTE_API_KEY = ""
DEFAULT_DIFY_REMOTE_TIMEOUT_SECONDS = 30
DEFAULT_DIFY_REMOTE_RESPONSE_MODE = "blocking"
DEFAULT_DIFY_REMOTE_USER = "zongziledger-local"
DEFAULT_DIFY_REMOTE_USER_AGENT = "python-requests/2.32.3"
DEFAULT_DIFY_REMOTE_INPUT_PAYLOAD_KEY = "payload_json"
DEFAULT_DIFY_REMOTE_INPUT_MESSAGES_KEY = "messages_json"
DEFAULT_DIFY_REMOTE_INPUTS_AS_JSON_TEXT = True

DEFAULT_KNOWN_STORE_LOOKUP_ENABLED = False
DEFAULT_KNOWN_STORE_LOOKUP_PATH = "data/known_stores.local.json"
DEFAULT_KNOWN_STORE_LOOKUP_MAX_MATCHES = 3


def resolve_start_marker(config: Mapping[str, Any]) -> str:
    start_marker = str(config.get("record_start_marker", config.get("prefix", DEFAULT_START_MARKER))).strip()
    return start_marker.lstrip("#＃").strip() or DEFAULT_START_MARKER


def resolve_end_marker(config: Mapping[str, Any], fallback: str = DEFAULT_END_MARKER) -> str:
    end_marker = str(config.get("record_end_marker", fallback)).strip()
    return end_marker or fallback
