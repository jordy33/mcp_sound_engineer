"""Smoke test: analysis on a synthetic file + the MCP handshake. No network."""
import json
import os
import subprocess
import sys
import tempfile
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
            answers[key] = {"score": 0.0}
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
                     "judge_master", "judge_mix", "validate_recipe", "engineer_info"}
    info = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "engineer_info", "arguments": {}}})
    assert info["result"]["isError"] is False
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
    assert engineer_server.handle({"method": "tools/call",
                                   "params": []})["error"]["code"] == -32602


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
