from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib import error, parse, request


DEFAULT_TIMEOUT_SECONDS = 20


class WetraceClientError(RuntimeError):
    """Raised when calling wetrace API fails."""


def _to_text(value: Any) -> str:
    return str(value or "").strip()


def _to_int(value: Any, default: int = 0, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = default
    if minimum is not None and parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


def _preview_text(value: str, limit: int = 240) -> str:
    text = _to_text(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _sanitize_base_url(base_url: str) -> str:
    value = _to_text(base_url)
    if not value:
        raise WetraceClientError("wetrace_base_url is empty")
    if value.endswith("/"):
        return value[:-1]
    return value


def _normalize_query(query: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    if not query:
        return {}
    normalized: Dict[str, str] = {}
    for key, value in query.items():
        key_text = _to_text(key)
        if not key_text:
            continue
        if value is None:
            continue
        if isinstance(value, bool):
            normalized[key_text] = "true" if value else "false"
            continue
        value_text = _to_text(value)
        if not value_text:
            continue
        normalized[key_text] = value_text
    return normalized


def _extract_data_list(payload: Any, keys: Sequence[str]) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        if all(k in payload for k in ("offset", "limit")) and isinstance(payload.get("data"), list):
            return [dict(item) for item in payload.get("data", []) if isinstance(item, dict)]
        if isinstance(payload.get("items"), list):
            return [dict(item) for item in payload.get("items", []) if isinstance(item, dict)]
        return [dict(payload)]

    return []


@dataclass
class WetraceClientConfig:
    base_url: str
    auth_token: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    chatrooms_path: str = "/api/v1/chatrooms"
    messages_path: str = "/api/v1/messages"
    sync_path: str = "/api/v1/system/sync"


class WetraceClient:
    """Minimal HTTP client for local wetrace service."""

    def __init__(self, config: WetraceClientConfig) -> None:
        self.base_url = _sanitize_base_url(config.base_url)
        self.auth_token = _to_text(config.auth_token)
        self.timeout_seconds = _to_int(config.timeout_seconds, DEFAULT_TIMEOUT_SECONDS, minimum=1, maximum=120)
        self.chatrooms_path = _to_text(config.chatrooms_path) or "/api/v1/chatrooms"
        self.messages_path = _to_text(config.messages_path) or "/api/v1/messages"
        self.sync_path = _to_text(config.sync_path) or "/api/v1/system/sync"

    def list_chatrooms(
        self,
        keyword: str = "",
        limit: int = 20,
        offset: int = 0,
        extras: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"limit": _to_int(limit, 20, minimum=1, maximum=500), "offset": _to_int(offset, 0, minimum=0)}
        keyword_text = _to_text(keyword)
        if keyword_text:
            query["keyword"] = keyword_text
        if extras:
            query.update(dict(extras))

        payload = self._request_json("GET", self.chatrooms_path, query=query)
        return _extract_data_list(payload, keys=("chatrooms", "items", "list", "rows", "data"))

    def list_messages(self, query: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
        payload = self._request_json("GET", self.messages_path, query=query)
        return _extract_data_list(payload, keys=("messages", "items", "list", "rows", "records", "data"))

    def trigger_sync(self) -> Any:
        return self._request_json("POST", self.sync_path, payload={})

    def _request_json(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        path_text = _to_text(path)
        if not path_text.startswith("/"):
            path_text = "/" + path_text

        query_payload = _normalize_query(query)
        full_url = self.base_url + path_text
        if query_payload:
            full_url += "?" + parse.urlencode(query_payload, doseq=True)

        headers = {"Accept": "application/json"}
        raw_payload: Optional[bytes] = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            raw_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.auth_token:
            headers["X-Auth-Token"] = self.auth_token

        req = request.Request(full_url, data=raw_payload, method=method.upper(), headers=headers)

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
                status_code = int(getattr(resp, "status", 200))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise WetraceClientError(
                f"wetrace HTTP {exc.code} {method.upper()} {full_url}: {_preview_text(body)}"
            ) from exc
        except error.URLError as exc:
            raise WetraceClientError(f"wetrace request failed {method.upper()} {full_url}: {exc.reason}") from exc
        except Exception as exc:
            raise WetraceClientError(f"wetrace request error {method.upper()} {full_url}: {exc}") from exc

        try:
            parsed = json.loads(raw_text) if raw_text else {}
        except Exception as exc:
            raise WetraceClientError(
                f"wetrace returned non-JSON (HTTP {status_code}) {method.upper()} {full_url}: {_preview_text(raw_text)}"
            ) from exc

        if isinstance(parsed, dict) and "success" in parsed:
            if not bool(parsed.get("success")):
                message = _to_text(parsed.get("message")) or "unknown_error"
                raise WetraceClientError(
                    f"wetrace API error {method.upper()} {full_url}: {message}"
                )
            return parsed.get("data")

        return parsed
