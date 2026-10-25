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

from analysis import analyze_master  # noqa: E402


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
    assert names == {"analyze_master", "analyze_stems", "judge_master", "judge_mix", "engineer_info"}
    info = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "engineer_info", "arguments": {}}})
    assert info["result"]["isError"] is False
    proc.stdin.close(); proc.wait(timeout=5)
    print("mcp handshake ok:", sorted(names))


if __name__ == "__main__":
    test_analyze_master()
    test_mcp_handshake()
    print("\ntodo OK")
