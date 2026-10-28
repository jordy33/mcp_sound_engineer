"""Minimal Jev (TypeSafe AI) client: typed decisions over a state."""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_LIVE_ENDPOINT = "https://jevtypesafeai.com/api/v1/decide"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 429 and 5xx are transient; any other 4xx is a request problem that a retry
# would only repeat. Timeouts and connection failures are also transient. The
# number of attempts and the total wall-clock budget are both bounded, so a
# stalled endpoint cannot outlive a typical MCP client timeout.
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 0.5
RETRY_MAX_DELAY_S = 5.0
DEFAULT_TIMEOUT_S = 20.0
MAX_TOTAL_SECONDS = 45.0


class JevError(RuntimeError):
    pass


def _strip_inline_comment(text: str) -> str:
    """Remove a `#` comment that is outside single or double quotes."""
    quote = None
    kept = []
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
            kept.append(char)
        elif char in ("'", '"'):
            quote = char
            kept.append(char)
        elif char == "#":
            break
        else:
            kept.append(char)
    return "".join(kept)


def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None
    key, raw = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    raw = _strip_inline_comment(raw).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    return key, raw


def _load_env() -> dict:
    """Read .env from the repo root, then the real environment (env wins)."""
    values: dict[str, str] = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as handle:
            for line in handle:
                parsed = _parse_env_line(line)
                if parsed is not None:
                    values[parsed[0]] = parsed[1]
    values.update({k: os.environ[k] for k in
                   ("JEV_API_KEY", "JEV_ENDPOINT", "JEV_MODEL") if k in os.environ})
    return values


def _default_endpoint(key: str) -> str:
    # `jv_live_` keys are served by the Jev playground; `apikey_` and unknown
    # prefixes use the public SystemOne API.
    return JEV_LIVE_ENDPOINT if key.startswith("jv_live_") else DEFAULT_ENDPOINT


def _validate_endpoint(url: str) -> str:
    """https everywhere except loopback, where local tests may use http."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port                       # malformed ports raise here
    except ValueError as exc:
        raise JevError(f"invalid JEV_ENDPOINT {url!r}: {exc}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise JevError(f"invalid JEV_ENDPOINT {url!r}: expected http(s)://host/path")
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise JevError(f"JEV_ENDPOINT {url!r} uses http; only loopback may skip TLS")
    return url


def config() -> dict:
    env = _load_env()
    key = env.get("JEV_API_KEY", "")
    return {
        "key": key,
        "endpoint": env.get("JEV_ENDPOINT", "") or _default_endpoint(key),
        "model": env.get("JEV_MODEL", "jev-latest"),
    }


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Honor Retry-After (seconds or HTTP date), capped so a retry stays bounded."""
    headers = exc.headers
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(retry_after)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                delay = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
        return min(max(delay, 0.0), RETRY_MAX_DELAY_S)
    return min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), RETRY_MAX_DELAY_S)


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode(errors="replace")[:300]
    except Exception:                      # the body is best-effort context
        return ""


def _transport_delay(attempt: int) -> float:
    return min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), RETRY_MAX_DELAY_S)


def _wait_before_retry(delay: float, deadline: float) -> None:
    """Sleep only if another attempt still fits inside the total budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 or delay >= remaining:
        raise JevError("Jev retry deadline exceeded")
    time.sleep(delay)


def _post(url: str, key: str, body: dict, timeout: float,
          deadline: float | None = None) -> bytes:
    data = json.dumps(body).encode()
    deadline = time.monotonic() + MAX_TOTAL_SECONDS if deadline is None else deadline
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JevError("Jev retry deadline exceeded")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=max(0.1, min(timeout, remaining))) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            transient = exc.code == 429 or 500 <= exc.code < 600
            if transient and attempt < MAX_ATTEMPTS:
                _wait_before_retry(_retry_delay(exc, attempt), deadline)
                continue
            raise JevError(f"Jev HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(_transport_delay(attempt), deadline)
                continue
            raise JevError(f"Jev unreachable: {exc.reason}") from exc
        except http.client.HTTPException as exc:
            # RemoteDisconnected inherits from both ConnectionResetError and
            # BadStatusLine; name it as a protocol error, not a generic OSError.
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(_transport_delay(attempt), deadline)
                continue
            raise JevError(f"Jev protocol error: {type(exc).__name__}: {exc}") from exc
        except TimeoutError as exc:
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(_transport_delay(attempt), deadline)
                continue
            raise JevError(f"Jev request failed: {type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(_transport_delay(attempt), deadline)
                continue
            raise JevError(f"Jev request failed: {type(exc).__name__}: {exc}") from exc
        except ValueError as exc:
            raise JevError(f"invalid Jev endpoint: {exc}") from exc
    raise JevError("Jev retry budget exhausted")   # unreachable, loop always returns


def ask(state: dict, questions: dict, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Send one decision request. Returns the complete `answers` map.

    Transport failures become JevError so the MCP server reports a domain error
    instead of surfacing urllib internals. Transient HTTP codes, timeouts and
    connection failures are retried within a bounded total deadline. A response
    that omits any question is an error: a missing answer must not read as
    "nothing to fix".
    """
    cfg = config()
    if not cfg["key"]:
        raise JevError("JEV_API_KEY is not set (copy .env.example to .env)")
    endpoint = _validate_endpoint(cfg["endpoint"])
    body = {"model": cfg["model"], "state": state, "questions": questions}
    raw = _post(endpoint, cfg["key"], body, timeout)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JevError(f"Jev returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "answers" not in payload:
        raise JevError(f"unexpected Jev response: {str(payload)[:300]}")
    answers = payload["answers"]
    if not isinstance(answers, dict):
        raise JevError(f"unexpected Jev answers: {str(answers)[:300]}")
    missing = [name for name in questions if name not in answers]
    if missing:
        raise JevError(f"Jev response is missing answers for {missing}")
    return answers
