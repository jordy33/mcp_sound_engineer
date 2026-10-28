"""Smoke test: analysis on a synthetic file + the MCP handshake. No network."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import engineer_server  # noqa: E402
from analysis import analyze_master, analyze_reference  # noqa: E402


def _start_server():
    return subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "engineer_server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def _send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n"); proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def _send_raw(proc, text):
    proc.stdin.write(text + "\n"); proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def _master_answers():
    answers = {}
    for key, spec in engineer_server.MASTER_QUESTIONS.items():
        if spec["type"] == "score":
            answers[key] = {"score": engineer_server.rules.SCORE_MIN}
        else:
            answers[key] = {"choice": next(iter(spec["criteria"]))}
    return answers


def make_wav(path: str, seconds: float = 3.0, sr: int = 44100) -> None:
    t = np.arange(int(seconds * sr)) / sr
    x = 0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * np.random.default_rng(1).normal(size=len(t))
    stereo = np.stack([x, x * 0.9], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def test_analyze_master():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.wav")
        make_wav(path)
        facts = analyze_master(path)
    assert facts["sample_rate"] == 44100
    assert facts["channels"] == 2
    assert set(facts["spectral"]["bands_db_rel"]) == {"sub", "bass", "low_mid", "mid", "high_mid", "high"}
    assert -40 < facts["loudness"]["integrated_lufs"] < 0
    assert facts["loudness"]["true_peak_dbtp"] <= 1.0
    assert np.isfinite(facts["spectral"]["sibilance_db_rel"])
    print("analyze_master ok:", facts["loudness"])


def test_mcp_handshake():
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "engineer_server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n"); proc.stdin.flush()
        return json.loads(proc.stdout.readline())
    init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "mcp_sound_engineer"
    listing = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in listing["result"]["tools"]}
    assert names == {"analyze_master", "analyze_stems", "analyze_sections", "analyze_reference",
                     "judge_master", "judge_mix", "validate_recipe", "verify_recipe",
                     "engineer_info"}
    info = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "engineer_info", "arguments": {}}})
    assert info["result"]["isError"] is False
    payload = json.loads(info["result"]["content"][0]["text"])
    assert payload["version"] == engineer_server.SERVER_VERSION, payload
    assert payload["rules_version"] == engineer_server.rules.RULES_VERSION, payload
    proc.stdin.close(); proc.wait(timeout=5)
    print("mcp handshake ok:", sorted(names))


def test_malformed_json_rpc_does_not_kill_the_loop():
    proc = _start_server()
    try:
        assert _send(proc, [1, 2, 3])["error"]["code"] == -32600
        assert _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": [1, 2]})["error"]["code"] == -32602
        assert _send_raw(proc, "{not json")["error"]["code"] == -32700
        assert _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping",
                            "params": {}})["result"] == {}
        assert proc.poll() is None, "the stdio loop died on malformed input"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
    print("malformed JSON-RPC survives ok")


def test_tools_call_arguments_must_be_an_object():
    proc = _start_server()
    try:
        reply = _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "engineer_info", "arguments": [1]}})
        assert reply["error"]["code"] == -32602, reply
        assert _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping",
                            "params": {}})["result"] == {}
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_handle_rejects_non_object_and_bad_params():
    assert engineer_server.handle([1, 2, 3])["error"]["code"] == -32600
    assert engineer_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": []})["error"]["code"] == -32602


def test_notifications_are_never_answered():
    """JSON-RPC forbids replying to a notification, including with an error."""
    for method in ("notifications/initialized", "notifications/cancelled",
                   "notifications/progress", "notifications/roots/list_changed",
                   "custom/notification"):
        assert engineer_server.handle(
            {"jsonrpc": "2.0", "method": method, "params": {}}) is None, method
    # A mistakenly supplied id does not make an MCP notification answerable.
    assert engineer_server.handle({"jsonrpc": "2.0", "id": 9,
                                   "method": "notifications/cancelled",
                                   "params": {}}) is None


def test_notification_does_not_shift_the_next_response():
    proc = _start_server()
    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                     "method": "notifications/cancelled",
                                     "params": {}}) + "\n")
        proc.stdin.flush()
        reply = _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        assert reply == {"jsonrpc": "2.0", "id": 1, "result": {}}, reply
        assert proc.poll() is None
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_missing_tool_argument_is_a_tool_error_not_a_crash():
    """A KeyError inside a handler must not kill the stdio loop."""
    proc = _start_server()
    try:
        reply = _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "analyze_master", "arguments": {}}})
        assert reply["result"]["isError"] is True, reply
        assert "path" in reply["result"]["content"][0]["text"], reply
        ping = _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        assert ping["result"] == {}, ping
        assert proc.poll() is None
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def test_verify_recipe_checks_measurements_after_the_bounce():
    with tempfile.TemporaryDirectory() as tmp:
        before = os.path.join(tmp, "before.wav")
        after = os.path.join(tmp, "after.wav")
        make_wav(before)
        make_wav(after)
        recipe = [
            {"id": "mid", "action": "eq.bell", "target": "master",
             "schema": engineer_server.rules.RECIPE_SCHEMA_VERSION,
             "freq_hz": 1250, "gain_db": -2.0, "q": 4.0, "role": "corrective",
             "expect": {"metric": "spectral.bands_db_rel.mid", "delta_db": 0.0,
                        "tolerance_db": 1.0}},
            {"id": "missing", "action": "eq.bell", "target": "master",
             "schema": engineer_server.rules.RECIPE_SCHEMA_VERSION,
             "freq_hz": 4000, "gain_db": -1.0, "q": 4.0, "role": "corrective",
             "expect": {"metric": "spectral.not_measured", "delta_db": 0.0,
                        "tolerance_db": 1.0}},
        ]
        result = engineer_server.tool_verify_recipe(recipe, before, after)
    checks = {c["id"]: c for c in result["checks"]}
    assert result["valid"] is True, result
    assert checks["mid"]["status"] == "met", checks["mid"]
    assert checks["missing"]["status"] == "unmeasured", checks["missing"]
    assert result["met"] == 1 and result["unmeasured"] == 1, result
    assert result["all_met"] is False, result


def test_judge_master_names_the_generic_reference_fallback():
    facts = {
        "file": "/tmp/master.wav", "sample_rate": 44100, "channels": 2,
        "duration_seconds": 60.0, "bit_depth": 24,
        "loudness": {"integrated_lufs": -18.0, "true_peak_dbtp": -3.0,
                     "loudness_range_lu": 9.0, "sample_peak_dbfs": -3.0,
                     "crest_db": 10.0},
        "spectral": {"bands_db_rel": {"sub": -20.0, "bass": -10.0, "low_mid": -12.0,
                                      "mid": -8.0, "high_mid": -13.0, "high": -17.0},
                     "sibilance_db_rel": -20.0},
        "stereo": {"phase_correlation": 0.9, "mono_cancel_db": -0.5},
        "clipping": {"count": 0, "flat_top_frames": 0, "samples": 0, "proportion": 0.0},
        "noise": {"floor_dbfs": None},
        "bandwidth": {"lossy_suspected": False, "drop_db": 0.0, "cutoff_hz": 20000},
    }
    captured = {}
    original_analyze = engineer_server.analyze_master
    original_ask = engineer_server.ask

    def fake_analyze(path):
        return dict(facts)

    def fake_ask(state, questions):
        captured.update(state)
        return _master_answers()

    engineer_server.analyze_master = fake_analyze
    engineer_server.ask = fake_ask
    try:
        result = engineer_server.tool_judge_master("/tmp/master.wav")
    finally:
        engineer_server.analyze_master = original_analyze
        engineer_server.ask = original_ask

    assert result["reference_source"] == "generic_default", result
    assert captured["reference_source"] == "generic_default", captured
    assert "reference_note" in captured, captured
    assert any("generic fallback" in w for w in result["warnings"]), result["warnings"]


def test_judge_master_passes_measured_reference_and_intent():
    captured = {}
    original_ask = engineer_server.ask
    original_builder = engineer_server.rules.build_master_recipe

    def fake_ask(state, questions):
        return _master_answers()

    def spy(answers, facts, platform, reference_curve=None, desired_lufs=None):
        captured.update(reference_curve=reference_curve, desired_lufs=desired_lufs)
        return []

    engineer_server.ask = fake_ask
    engineer_server.rules.build_master_recipe = spy
    try:
        with tempfile.TemporaryDirectory() as tmp:
            master = os.path.join(tmp, "master.wav")
            reference = os.path.join(tmp, "reference.wav")
            make_wav(master)
            make_wav(reference)
            expected_curve = analyze_reference(reference)["reference_curve"]
            result = engineer_server.tool_judge_master(master, "spotify", reference,
                                                       None, -14.0)
    finally:
        engineer_server.ask = original_ask
        engineer_server.rules.build_master_recipe = original_builder

    assert captured["reference_curve"] == expected_curve, captured
    assert captured["desired_lufs"] == -14.0, captured
    assert result["reference_curve"] == expected_curve, result
    assert result["recipe"] == [], result


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/health")
            conn.getresponse().read()
            conn.close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the HTTP server did not start")


def test_judge_master_short_file_returns_a_terminal_recipe():
    """A <3 s file has no LRA; the gate refuses it instead of crashing."""
    original_ask = engineer_server.ask
    engineer_server.ask = lambda state, questions: _master_answers()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "short.wav")
            make_wav(path, seconds=1.0)
            result = engineer_server.tool_judge_master(path)
    finally:
        engineer_server.ask = original_ask
    recipe = result["recipe"]
    assert len(recipe) == 1 and recipe[0]["action"] == "send_back", recipe
    assert any("unmeasured:loudness.loudness_range_lu" in r for r in recipe[0]["reasons"]), recipe


def test_http_rejects_foreign_host_origin_and_oversized_body():
    port = _free_port()
    proc = subprocess.Popen([sys.executable,
                             os.path.join(ROOT, "server", "engineer_server.py"),
                             "--http", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_health(port)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        health = conn.getresponse()
        body = json.loads(health.read())
        conn.close()
        assert health.status == 200 and body["version"] == engineer_server.SERVER_VERSION, body

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health", headers={"Host": "evil.example"})
        assert conn.getresponse().status == 403, "a foreign Host must be refused"
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        conn.request("POST", "/mcp", body=payload,
                     headers={"Content-Type": "application/json",
                              "Origin": "https://evil.example"})
        assert conn.getresponse().status == 403, "a cross-site Origin must be refused"
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/mcp", body=payload,
                     headers={"Content-Type": "application/json",
                              "Origin": "http://localhost:3000"})
        response = conn.getresponse()
        reply = json.loads(response.read())
        conn.close()
        assert response.status == 200 and reply["result"] == {}, reply

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/mcp")
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Content-Length", str(engineer_server.MAX_BODY_BYTES + 1))
        conn.endheaders()
        assert conn.getresponse().status == 413, "an oversized body must be refused"
        conn.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


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
