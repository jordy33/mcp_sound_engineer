"""Small, honest MCP client for the mcp_logic HTTP bridge.

The previous adapter sent ``tools/call`` as its first message.  mcp_logic's
server is a real MCP server: it expects ``initialize`` and then the
``notifications/initialized`` notification before any tool call, and it may
return an ``Mcp-Session-Id`` that every later request must echo.

This client keeps three things separate:

* transport success (did an HTTP reply arrive?),
* JSON-RPC success (matching id, no ``error`` object),
* Honest Contract state (``success``/``verified``/``state``/``reason``/``error``).

Only the last one authorises a write.  ``ToolResult.applied`` is true only for
State A with ``verified: true`` and, for ``logic_plugins.*``, ``hc_schema: 2``.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .mcp_logic_contract import (
    HC_V2_SCHEMA,
    HC_V2_TOOLS,
    TOOL_AUDIO,
    TOOL_MIXER,
    TOOL_PLUGINS,
    TOOL_PROJECT,
)

PROTOCOL_VERSION = "2025-03-26"
CLIENT_NAME = "mcp_sound_engineer"
CLIENT_VERSION = "0.6.1"

DEFAULT_TIMEOUT = 60
WRITE_TIMEOUT = 120
EXPORT_TIMEOUT = 600
ANALYSIS_TIMEOUT = 120

_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class BridgeError(Exception):
    """The bridge could not be reached or the MCP handshake failed."""


@dataclass
class HttpResponse:
    status: int
    headers: dict
    body: bytes


def urllib_transport(method: str, url: str, headers: dict, data: bytes | None,
                     timeout: float) -> HttpResponse:
    """Default transport.  HTTP error bodies are returned, not raised."""
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status,
                                {key.lower(): value for key, value in response.headers.items()},
                                response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return HttpResponse(exc.code,
                            {key.lower(): value for key, value in (exc.headers or {}).items()},
                            body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BridgeError("cannot reach %s: %s" % (url, exc))


@dataclass
class ToolResult:
    ok: bool
    success: bool | None = None
    state: str | None = None
    verified: bool | None = None
    hc_schema: int | None = None
    reason: str | None = None
    error: str | None = None
    payload: dict = field(default_factory=dict)
    raw_text: str = ""
    tool: str | None = None
    command: str | None = None
    is_error: bool = False

    @property
    def state_a(self) -> bool:
        """The only shape that authorises a subsequent action."""
        if not self.ok or self.is_error:
            return False
        if self.success is not True or self.verified is not True or self.state != "A":
            return False
        if self.tool in HC_V2_TOOLS and self.hc_schema != HC_V2_SCHEMA:
            return False
        return True

    @property
    def applied(self) -> bool:
        return self.state_a

    def require_state_a(self, what: str = "operation") -> "ToolResult":
        if not self.state_a:
            raise BridgeError("%s did not reach State A: %s" % (what, self.describe()))
        return self

    def describe(self) -> str:
        parts = ["ok=%s" % self.ok]
        if self.is_error:
            parts.append("isError=true")
        if self.state is not None:
            parts.append("state=%s" % self.state)
        if self.verified is not None:
            parts.append("verified=%s" % self.verified)
        if self.hc_schema is not None:
            parts.append("hc_schema=%s" % self.hc_schema)
        if self.reason:
            parts.append("reason=%s" % self.reason)
        if self.error:
            parts.append("error=%s" % self.error)
        return ", ".join(parts)


def _truth(value):
    return value if isinstance(value, bool) else None


def _text(value):
    return value if isinstance(value, str) else None


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content_text(result: dict) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            if isinstance(item.get("text"), str):
                return item["text"]
    return ""


def _json_from_text(text: str):
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_payload(result: dict) -> tuple[dict, str]:
    """Prefer structuredContent; fall back to content[0].text parsed as JSON."""
    text = _content_text(result)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured, text or json.dumps(structured)
    parsed = _json_from_text(text)
    if parsed is not None:
        return parsed, text
    return {}, text


def _sse_messages(body: str):
    """Yield JSON objects from SSE ``data:`` lines (keepalives ignored)."""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if isinstance(message, dict):
            yield message


class McpClient:
    def __init__(self, endpoint: str, timeout: float = DEFAULT_TIMEOUT,
                 transport=urllib_transport, health_check: bool = True):
        self.endpoint = endpoint
        self.timeout = timeout
        self._transport = transport
        self._health_check_enabled = health_check
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 0
        self._lock = threading.Lock()
        self.protocol_version: str | None = None
        self.server_info: dict = {}

    @property
    def base_url(self) -> str:
        if self.endpoint.endswith("/mcp"):
            return self.endpoint[:-4]
        return self.endpoint.rstrip("/")

    # -- handshake ---------------------------------------------------------
    def health_check(self, timeout: float = 5) -> dict:
        reply = self._transport("GET", self.base_url + "/health",
                                {"Accept": "application/json"}, None, timeout)
        if reply.status != 200:
            raise BridgeError("health check failed with HTTP %s" % reply.status)
        try:
            data = json.loads(reply.body.decode() or "{}")
        except ValueError:
            raise BridgeError("health check returned non-JSON")
        if data.get("ok") is not True:
            raise BridgeError(
                "the mcp_logic bridge is up but its upstream process is not running")
        return data

    def initialize(self) -> "McpClient":
        with self._lock:
            if self._initialized:
                return self
            if self._health_check_enabled:
                self.health_check()
            payload = self._rpc_request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            }, self.timeout)
            result = payload.get("result")
            if payload.get("error") is not None or not isinstance(result, dict):
                raise BridgeError("initialize failed: %s"
                                  % (payload.get("error") or "no result"))
            self.protocol_version = _text(result.get("protocolVersion"))
            self.server_info = result.get("serverInfo") or {}
            self._notify("notifications/initialized")
            self._initialized = True
            return self

    # -- JSON-RPC ----------------------------------------------------------
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _post(self, payload: dict, timeout: float) -> HttpResponse:
        headers = dict(_JSON_HEADERS)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        data = json.dumps(payload).encode()
        reply = self._transport("POST", self.endpoint, headers, data, timeout)
        session = reply.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        return reply

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        reply = self._post(payload, self.timeout)
        if reply.status >= 400:
            raise BridgeError("notification %s failed with HTTP %s"
                              % (method, reply.status))

    def _rpc_request(self, method: str, params: dict, timeout: float) -> dict:
        request_id = self._new_id()
        payload = {"jsonrpc": "2.0", "id": request_id,
                   "method": method, "params": params}
        reply = self._post(payload, timeout)
        return self._decode(reply, request_id)

    def _decode(self, reply: HttpResponse, expected_id: int) -> dict:
        content_type = (reply.headers.get("content-type") or "").lower()
        messages = []
        if "text/event-stream" in content_type:
            messages = list(_sse_messages(reply.body.decode(errors="replace")))
        else:
            try:
                decoded = json.loads(reply.body.decode() or "{}")
            except ValueError:
                raise BridgeError("HTTP %s returned non-JSON" % reply.status)
            messages = [decoded] if isinstance(decoded, dict) else []
        for message in messages:
            if message.get("id") == expected_id:
                return message
        if reply.status >= 400:
            raise BridgeError("HTTP %s without a matching JSON-RPC reply"
                              % reply.status)
        raise BridgeError("no JSON-RPC reply with id %s" % expected_id)

    # -- tools -------------------------------------------------------------
    def call_tool(self, tool: str, arguments: dict,
                  timeout: float | None = None) -> ToolResult:
        self.initialize()
        command = arguments.get("command") if isinstance(arguments, dict) else None
        limit = timeout if timeout is not None else _default_timeout(tool)
        payload = self._rpc_request(
            "tools/call", {"name": tool, "arguments": arguments}, limit)
        return _tool_result(payload, tool, command)

    def read_resource(self, uri: str, timeout: float | None = None) -> ToolResult:
        self.initialize()
        payload = self._rpc_request(
            "resources/read", {"uri": uri},
            timeout if timeout is not None else self.timeout)
        if payload.get("error") is not None:
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            return ToolResult(ok=False, error=message or "JSON-RPC error",
                              payload=payload, tool="resources/read")
        result = payload.get("result") or {}
        contents = result.get("contents") if isinstance(result, dict) else None
        text = ""
        if isinstance(contents, list):
            for item in contents:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text = item["text"]
                    break
        parsed = _json_from_text(text) or {}
        return ToolResult(ok=True, payload=parsed, raw_text=text,
                          tool="resources/read", command=uri)

    def get_inventory(self, selector: dict,
                      timeout: float | None = None) -> ToolResult:
        if not isinstance(selector, dict):
            raise ValueError("inventory selector must be an object")
        if ("track" in selector) == ("strip" in selector):
            raise ValueError("inventory selector needs exactly one of track or strip")
        return self.call_tool(
            TOOL_PLUGINS, {"command": "get_inventory", "params": dict(selector)},
            timeout if timeout is not None else DEFAULT_TIMEOUT)

    def close(self) -> None:
        """Best-effort DELETE; the bridge has no server-side session state."""
        if self._initialized:
            try:
                self._transport("DELETE", self.endpoint, {}, None, 5)
            except BridgeError:
                pass


def _default_timeout(tool: str) -> float:
    if tool == TOOL_PROJECT:
        return EXPORT_TIMEOUT
    if tool == TOOL_AUDIO:
        return ANALYSIS_TIMEOUT
    if tool == TOOL_PLUGINS:
        return WRITE_TIMEOUT
    if tool == TOOL_MIXER:
        return WRITE_TIMEOUT
    return DEFAULT_TIMEOUT


def _tool_result(payload: dict, tool: str, command) -> ToolResult:
    if payload.get("error") is not None:
        error = payload["error"]
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or "JSON-RPC error"
        else:
            message = str(error)
        return ToolResult(ok=False, error=str(message), payload=payload,
                          tool=tool, command=command)
    result = payload.get("result")
    if not isinstance(result, dict):
        return ToolResult(ok=False, error="JSON-RPC result is not an object",
                          payload=payload, tool=tool, command=command,
                          raw_text=json.dumps(payload))
    data, text = _extract_payload(result)
    is_error = result.get("isError") is True
    state = _text(data.get("state"))
    verified = _truth(data.get("verified"))
    success = _truth(data.get("success"))
    error = _text(data.get("error"))
    reason = _text(data.get("reason"))
    if is_error and not error:
        error = "tool reported isError"
    return ToolResult(
        ok=True,
        success=success,
        state=state,
        verified=verified,
        hc_schema=_integer(data.get("hc_schema")),
        reason=reason,
        error=error,
        payload=data,
        raw_text=text,
        tool=tool,
        command=command,
        is_error=is_error,
        # keep the JSON-RPC id available to callers without another field
    )


_clients: dict[str, McpClient] = {}
_clients_lock = threading.Lock()


def connect(endpoint: str, timeout: float = 30,
            health_check: bool = True) -> McpClient:
    client = McpClient(endpoint, timeout=timeout, health_check=health_check)
    client.initialize()
    return client


def _cached_client(endpoint: str, timeout: float = DEFAULT_TIMEOUT) -> McpClient:
    with _clients_lock:
        client = _clients.get(endpoint)
        if client is None:
            client = McpClient(endpoint, timeout=timeout)
            _clients[endpoint] = client
        return client


def call_tool(endpoint: str, tool: str, arguments: dict,
              timeout: float | None = None) -> ToolResult:
    return _cached_client(endpoint).call_tool(tool, arguments, timeout)


def read_resource(endpoint: str, uri: str,
                  timeout: float | None = None) -> ToolResult:
    return _cached_client(endpoint).read_resource(uri, timeout)


def get_inventory(endpoint: str, selector: dict,
                  timeout: float | None = None) -> ToolResult:
    return _cached_client(endpoint).get_inventory(selector, timeout)
