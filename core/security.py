"""Small, dependency-free helpers for local service trust boundaries."""

from __future__ import annotations

import hmac
import ipaddress
from typing import Mapping, Any


def is_loopback_host(host: Any) -> bool:
    """Return True only for an unambiguous loopback host/address."""
    text = str(host or "").strip().strip("[]").casefold()
    if text == "localhost":
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def normalize_loopback_bind_host(host: Any, default: str = "127.0.0.1") -> str:
    """Normalize a bind address and reject non-loopback exposure."""
    text = str(host or "").strip()
    if not text:
        return default
    if text.casefold() == "localhost":
        return "127.0.0.1"
    if not is_loopback_host(text):
        raise ValueError("service bind host must be loopback (127.0.0.1 or ::1)")
    return text.strip("[]")


def client_is_loopback(client_address: Any) -> bool:
    try:
        host = client_address[0]
    except (IndexError, KeyError, TypeError):
        return False
    return is_loopback_host(host)


def bearer_token_matches(headers: Mapping[str, Any], expected: Any) -> bool:
    """Compare a Bearer token without exposing it or using timing-sensitive ==."""
    expected_text = str(expected or "").strip()
    if not expected_text:
        return False
    authorization = str(headers.get("Authorization", "") or "").strip()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected_text.encode("utf-8"))


def request_is_authorized(headers: Mapping[str, Any], client_address: Any, expected: Any) -> bool:
    """Require the configured token, or limit tokenless operation to loopback."""
    expected_text = str(expected or "").strip()
    if expected_text:
        return bearer_token_matches(headers, expected_text)
    return client_is_loopback(client_address)
