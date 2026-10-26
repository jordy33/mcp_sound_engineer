"""Transport and Honest Contract tests for adapter.mcp_client.

These use a fake transport, so no Logic project, bridge or network is needed.
The invariant under test is simple: only an explicit State A envelope with a
matching ``hc_schema`` (for ``logic_plugins.*``) may be treated as applied.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adapter.mcp_client import (  # noqa: E402
    BridgeError,
    HttpResponse,
    McpClient,
)
from adapter.mcp_logic_contract import (  # noqa: E402
    TOOL_AUDIO,
    TOOL_MIXER,
    TOOL_PLUGINS,
    TOOL_PROJECT,
)

ENDPOINT = "http://127.0.0.1:8765/mcp"
SESSION = "session-abc"
LONG_MARKER = "x" * 1200


# ---------------------------------------------------------------------------
# fake bridge
# ---------------------------------------------------------------------------
class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, method, url, headers, data, timeout):
        body = json.loads(data) if data else None
        record = {"method": method, "url": url, "headers": dict(headers),
                  "body": body, "timeout": timeout}
        self.requests.append(record)
        return self.handler(record)

    def jsonrpc(self):
        return [r for r in self.requests
                if r["method"] == "POST" and isinstance(r["body"], dict)
                and "id" in r["body"]]

    def tool_calls(self):
        return [r for r in self.requests
                if r["body"] and r["body"].get("method") == "tools/call"]


def _http(status, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    merged = {"content-type": "application/json"}
    merged.update(headers or {})
    return HttpResponse(status, merged, body)


def _reply(record, result, content_type="application/json"):
    return _http(200, {"jsonrpc": "2.0", "id": record["body"]["id"],
                       "result": result}, {"content-type": content_type})


def _text(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error}


def _hc(state="A", success=True, verified=True, hc_schema=2, **extra):
    envelope = {"success": success, "verified": verified, "state": state}
    if hc_schema is not None:
        envelope["hc_schema"] = hc_schema
    envelope.update(extra)
    return envelope


def _bridge(call_handler=None, health_ok=True, session=SESSION):
    """Handle handshake and health; delegate tools/call and resources/read."""
    def handler(record):
        if record["method"] == "GET":
            return _http(200, {"ok": health_ok, "transport": ["http"]})
        if record["method"] == "DELETE":
            return HttpResponse(204, {}, b"")
        message = record["body"]
        method = message.get("method")
        if method == "initialize":
            headers = {"mcp-session-id": session} if session else {}
            return _http(200, {"jsonrpc": "2.0", "id": message["id"],
                               "result": {"protocolVersion": "2025-03-26",
                                          "serverInfo": {"name": "mcp_logic"}}},
                         headers)
        if method == "notifications/initialized":
            return HttpResponse(202, {}, b"")
        if call_handler is None:
            return _reply(record, _text(_hc()))
        return call_handler(record)

    return FakeTransport(handler)


def _client(transport, timeout=30):
    return McpClient(ENDPOINT, timeout=timeout, transport=transport,
                     health_check=True)


def _call(client, tool=TOOL_PLUGINS, command="get_inventory", params=None):
    return client.call_tool(tool, {"command": command,
                                   "params": params or {"track": 3}})


def _raises(exc_type, fn, needle=None):
    try:
        fn()
    except exc_type as exc:
        if needle is not None:
            assert needle in str(exc), (needle, str(exc))
        return exc
    raise AssertionError("expected %s" % exc_type.__name__)


# ---------------------------------------------------------------------------
# handshake and session
# ---------------------------------------------------------------------------
def test_handshake_precedes_tool_call_and_session_is_echoed():
    transport = _bridge()
    client = _client(transport)
    result = _call(client)

    assert result.ok is True
    assert transport.requests[0]["method"] == "GET"
    assert transport.requests[0]["url"] == "http://127.0.0.1:8765/health"
    methods = [r["body"].get("method") for r in transport.jsonrpc()]
    assert methods == ["initialize", "tools/call"]
    notifications = [r["body"] for r in transport.requests
                     if r["body"] and r["body"].get("method") == "notifications/initialized"]
    assert len(notifications) == 1
    assert "id" not in notifications[0]
    assert methods.index("initialize") < 1

    initialize = transport.jsonrpc()[0]
    assert initialize["body"]["params"]["protocolVersion"] == "2025-03-26"
    assert initialize["body"]["params"]["clientInfo"]["name"] == "mcp_sound_engineer"
    assert "mcp-session-id" not in initialize["headers"]

    call = transport.tool_calls()[0]
    assert call["headers"].get("Mcp-Session-Id") == SESSION
    assert call["body"]["params"]["name"] == TOOL_PLUGINS
    assert call["body"]["params"]["arguments"] == {
        "command": "get_inventory", "params": {"track": 3}}
    assert client.protocol_version == "2025-03-26"


def test_initialize_runs_once_per_client():
    transport = _bridge()
    client = _client(transport)
    _call(client)
    _call(client)
    methods = [r["body"].get("method") for r in transport.jsonrpc()]
    assert methods.count("initialize") == 1
    assert methods.count("tools/call") == 2
    # Nothing between calls re-initializes or re-notifies.
    assert len([r for r in transport.requests
                if r["body"] and r["body"].get("method") == "notifications/initialized"]) == 1


def test_health_check_is_skipped_when_disabled():
    transport = _bridge()
    client = McpClient(ENDPOINT, transport=transport, health_check=False)
    _call(client)
    assert not [r for r in transport.requests if r["method"] == "GET"]


def test_health_failure_names_the_upstream():
    transport = _bridge(health_ok=False)
    client = _client(transport)
    _raises(BridgeError, lambda: _call(client), "upstream process is not running")
    assert not transport.tool_calls()


def test_notification_failure_aborts_initialization():
    transport = _bridge()
    original = transport.handler

    def handler(record):
        if record["body"] and record["body"].get("method") == "notifications/initialized":
            return HttpResponse(500, {}, b"")
        return original(record)

    transport.handler = handler
    client = _client(transport)
    _raises(BridgeError, lambda: _call(client), "notifications/initialized")
    assert not transport.tool_calls()
    assert client._initialized is False


# ---------------------------------------------------------------------------
# envelopes
# ---------------------------------------------------------------------------
def test_structured_content_is_preferred_over_text():
    structured = _hc()
    stale = _hc(state="C", success=False, verified=False,
                error="stale text should be ignored")
    transport = _bridge(lambda record: _reply(
        record, {"content": [{"type": "text", "text": json.dumps(stale)}],
                 "structuredContent": structured, "isError": False}))
    result = _call(_client(transport))
    assert result.state == "A"
    assert result.applied is True
    assert result.payload == structured


def test_text_content_is_parsed_without_truncation():
    envelope = _hc(long_field=LONG_MARKER)
    transport = _bridge(lambda record: _reply(record, _text(envelope)))
    result = _call(_client(transport))
    assert result.ok is True
    assert result.applied is True
    assert result.payload["long_field"] == LONG_MARKER
    assert LONG_MARKER in result.raw_text
    assert len(result.raw_text) > 300


def test_state_b_reason_is_not_applied():
    envelope = _hc(state="B", verified=False, reason="readback_mismatch")
    transport = _bridge(lambda record: _reply(record, _text(envelope)))
    result = _call(_client(transport))
    assert result.ok is True
    assert result.success is True
    assert result.state == "B"
    assert result.reason == "readback_mismatch"
    assert result.applied is False
    _raises(BridgeError, lambda: result.require_state_a("eq"), "readback_mismatch")


def test_state_c_error_is_not_applied():
    envelope = _hc(state="C", success=False, verified=False,
                   error="target_slot_no_longer_empty")
    transport = _bridge(lambda record: _reply(record, _text(envelope)))
    result = _call(_client(transport))
    assert result.state == "C"
    assert result.error == "target_slot_no_longer_empty"
    assert result.applied is False


def test_v2_envelope_without_hc_schema_is_not_applied():
    envelope = _hc(hc_schema=None)
    transport = _bridge(lambda record: _reply(record, _text(envelope)))
    result = _call(_client(transport))
    assert result.state == "A"
    assert result.verified is True
    assert result.hc_schema is None
    assert result.applied is False


def test_v2_envelope_with_wrong_hc_schema_is_not_applied():
    envelope = _hc(hc_schema=1)
    transport = _bridge(lambda record: _reply(record, _text(envelope)))
    result = _call(_client(transport))
    assert result.hc_schema == 1
    assert result.applied is False


def test_v1_mixer_envelope_is_applied_without_hc_schema():
    envelope = _hc(hc_schema=None)
    transport = _bridge(lambda record: _reply(record, _text(envelope)))
    result = _call(_client(transport), tool=TOOL_MIXER, command="set_volume",
                   params={"track": 3, "value": 0.5})
    assert result.hc_schema is None
    assert result.applied is True


def test_state_a_requires_success_and_verified():
    for envelope in (_hc(success=None), _hc(verified=None),
                     _hc(success=False), _hc(verified=False),
                     _hc(state="B")):
        transport = _bridge(lambda record, env=envelope: _reply(record, _text(env)))
        result = _call(_client(transport))
        assert result.applied is False, envelope


def test_is_error_true_is_not_applied_even_with_state_a():
    transport = _bridge(lambda record: _reply(
        record, _text(_hc(), is_error=True)))
    result = _call(_client(transport))
    assert result.is_error is True
    assert result.ok is True
    assert result.applied is False
    assert "isError" in result.error


# ---------------------------------------------------------------------------
# JSON-RPC and transport failure
# ---------------------------------------------------------------------------
def test_json_rpc_error_is_not_ok_and_keeps_payload():
    error = {"code": -32000, "message": "unknown_plugin_identity"}
    transport = _bridge(lambda record: _http(
        200, {"jsonrpc": "2.0", "id": record["body"]["id"], "error": error}))
    result = _call(_client(transport))
    assert result.ok is False
    assert result.error == "unknown_plugin_identity"
    assert result.payload["error"] == error
    assert result.applied is False


def test_http_error_with_matching_json_rpc_error_is_reported():
    transport = _bridge(lambda record: _http(
        504, {"jsonrpc": "2.0", "id": record["body"]["id"],
              "error": {"code": -32000, "message": "upstream timeout"}}))
    result = _call(_client(transport))
    assert result.ok is False
    assert result.error == "upstream timeout"
    assert result.applied is False


def test_http_error_without_matching_id_is_a_bridge_error():
    transport = _bridge(lambda record: _http(
        502, {"jsonrpc": "2.0", "id": 999999, "error": {"message": "bad gateway"}}))
    _raises(BridgeError, lambda: _call(_client(transport)), "HTTP 502")


def test_mismatched_response_id_is_rejected():
    transport = _bridge(lambda record: _http(
        200, {"jsonrpc": "2.0", "id": record["body"]["id"] + 1,
              "result": _text(_hc())}))
    _raises(BridgeError, lambda: _call(_client(transport)), "no JSON-RPC reply")


def test_non_json_reply_is_a_bridge_error():
    transport = _bridge(lambda record: _http(200, b"<html>nope</html>",
                                             {"content-type": "text/html"}))
    _raises(BridgeError, lambda: _call(_client(transport)), "non-JSON")


def test_sse_reply_is_parsed_and_id_matched():
    def handler(record):
        result = {"jsonrpc": "2.0", "id": record["body"]["id"],
                  "result": _text(_hc())}
        stale = {"jsonrpc": "2.0", "id": 999, "result": _text(_hc(state="C"))}
        body = ("event: message\n"
                "data: %s\n\n"
                ": keepalive\n\n"
                "event: message\n"
                "data: %s\n\n" % (json.dumps(stale), json.dumps(result)))
        return HttpResponse(200, {"content-type": "text/event-stream"},
                            body.encode())

    transport = _bridge(handler)
    result = _call(_client(transport))
    assert result.ok is True
    assert result.state == "A"
    assert result.applied is True


def test_write_is_never_retried():
    transport = _bridge(lambda record: _reply(
        record, _text(_hc(state="C", success=False, verified=False,
                          error="write_failed", write_attempted=True))))
    client = _client(transport)
    result = client.call_tool(TOOL_PLUGINS, {
        "command": "set_eq_band_verified",
        "params": {"track": 3, "insert": 2, "band": "Peak 1",
                   "parameter": "Gain", "value": -2, "unit": "dB"}})
    assert result.applied is False
    assert result.payload["write_attempted"] is True
    assert len(transport.tool_calls()) == 1


def test_call_tool_argument_shape_is_passed_verbatim():
    transport = _bridge()
    client = _client(transport)
    arguments = {"command": "compare_spectra",
                 "params": {"before_path": "/tmp/a.wav",
                            "after_path": "/tmp/b.wav"}}
    client.call_tool(TOOL_AUDIO, arguments)
    sent = transport.tool_calls()[0]["body"]["params"]
    assert sent["name"] == TOOL_AUDIO
    assert sent["arguments"] == arguments


# ---------------------------------------------------------------------------
# inventory, resources and timeouts
# ---------------------------------------------------------------------------
def test_get_inventory_requires_exactly_one_selector():
    transport = _bridge()
    client = _client(transport)
    _raises(ValueError, lambda: client.get_inventory({"track": 3, "strip": "output"}),
            "exactly one")
    _raises(ValueError, lambda: client.get_inventory({}), "exactly one")
    assert not transport.tool_calls()

    client.get_inventory({"strip": "output"})
    sent = transport.tool_calls()[0]["body"]["params"]["arguments"]
    assert sent == {"command": "get_inventory", "params": {"strip": "output"}}


def test_read_resource_parses_json_text():
    def handler(record):
        assert record["body"]["method"] == "resources/read"
        assert record["body"]["params"] == {"uri": "logic://mixer"}
        return _reply(record, {"contents": [
            {"uri": "logic://mixer", "mimeType": "application/json",
             "text": json.dumps({"tracks": [{"index": 3}]})}]})

    transport = _bridge(handler)
    result = _client(transport).read_resource("logic://mixer")
    assert result.ok is True
    assert result.payload == {"tracks": [{"index": 3}]}
    assert result.command == "logic://mixer"


def test_read_resource_error_is_not_ok():
    transport = _bridge(lambda record: _http(
        200, {"jsonrpc": "2.0", "id": record["body"]["id"],
              "error": {"message": "resource not found"}}))
    result = _client(transport).read_resource("logic://nope")
    assert result.ok is False
    assert result.error == "resource not found"


def test_default_timeouts_follow_operation_kind():
    transport = _bridge()
    client = _client(transport)
    client.call_tool(TOOL_PROJECT, {"command": "export_plan", "params": {}})
    client.call_tool(TOOL_AUDIO, {"command": "analyze_file", "params": {}})
    client.get_inventory({"track": 3})
    client.call_tool(TOOL_MIXER, {"command": "set_volume", "params": {}})
    client.call_tool(TOOL_PLUGINS, {"command": "set_eq_band_verified", "params": {}},
                     timeout=7)
    timeouts = [r["timeout"] for r in transport.tool_calls()]
    assert timeouts == [600, 120, 60, 120, 7]


def test_close_after_initialize_sends_one_delete():
    transport = _bridge()
    client = _client(transport)
    _call(client)
    client.close()
    deletes = [r for r in transport.requests if r["method"] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0]["url"] == ENDPOINT


def test_close_before_initialize_is_a_noop():
    transport = _bridge()
    McpClient(ENDPOINT, transport=transport).close()
    assert transport.requests == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ok  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} fallo(s)")
    sys.exit(1 if failures else 0)
