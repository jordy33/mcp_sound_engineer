"""Adapter tests: the plan must match mcp_logic's real verified surface."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))

import rules  # noqa: E402
from adapter.logic_adapter import (CAPABILITIES, MODE, db_to_fader,  # noqa: E402
                                   plan, summary)

PROJECT = "/tmp/session.logicx"
MASTER = {"index": 0, "name": "Stereo Out"}


def test_every_action_has_a_capability():
    missing = [a for a in rules.ACTIONS if a not in CAPABILITIES]
    assert not missing, missing


def test_db_to_fader_is_monotonic_and_clamped():
    assert db_to_fader(0) == 1.0
    assert abs(db_to_fader(-6) - 0.5012) < 0.001
    assert db_to_fader(6) == 1.0          # a boost cannot be expressed as 0..1
    assert db_to_fader(-80) >= 0.0


def test_fader_plan_uses_the_mixer_contract():
    steps = plan([{"action": "fader", "target": "vocal", "gain_db": -2.0}],
                 {"vocal": {"index": 3, "name": "Lead Vocal"}})
    assert steps[0]["status"] == "ready"
    assert steps[0]["calls"] == [{"tool": "logic_mixer",
                                  "arguments": {"command": "set_volume",
                                                "params": {"track": 3, "value": 0.7943}}}]


def test_eq_uses_engineering_units_not_raw_ax():
    steps = plan([{"action": "eq.bell", "target": "master", "freq_hz": 300,
                   "gain_db": -2.0, "q": 4.0}], master=MASTER, project_path=PROJECT)
    calls = steps[0]["calls"]
    assert [c["arguments"]["params"]["parameter"] for c in calls] == [
        "Frequency", "Gain", "Q-Factor"]
    assert [c["arguments"]["params"]["unit"] for c in calls] == ["Hz", "dB", "Q"]
    assert all(c["arguments"]["params"]["project_expected_path"] == PROJECT for c in calls)
    assert all(c["arguments"]["params"]["mode"] == MODE for c in calls)


def test_limiter_is_supported_through_the_verified_surface():
    steps = plan([{"action": "limiter", "target": "master", "ceiling_dbtp": -1.0}],
                 master=MASTER, project_path=PROJECT)
    assert steps[0]["status"] == "ready"
    commands = [c["arguments"]["command"] for c in steps[0]["calls"]]
    assert commands == ["insert_verified", "set_param_verified"]
    ceiling = steps[0]["calls"][1]["arguments"]["params"]
    assert ceiling["param"] == "outputCeiling" and ceiling["value"] == -1.0


def test_compressor_writes_ratio_attack_release():
    steps = plan([{"action": "compressor", "target": "mix_bus", "ratio": 2.0,
                   "attack_ms": 30, "release_ms": 250}],
                 master=MASTER, project_path=PROJECT)
    params = [c["arguments"]["params"].get("param") for c in steps[0]["calls"]]
    assert params == [None, "ratio", "attack", "release"], params


def test_polarity_uses_the_gain_plugin_phase_invert():
    steps = plan([{"action": "polarity.invert", "target": "stem",
                   "stem": "/b/01_bass.wav"}],
                 tracks={"01_bass.wav": {"index": 2, "name": "Bass"}}, project_path=PROJECT)
    calls = steps[0]["calls"]
    assert calls[0]["arguments"]["params"]["plugin_name"] == "Gain"
    assert calls[1]["arguments"]["params"]["param"] == "phaseInvert"


def test_de_esser_and_dither_are_unsupported():
    steps = plan([{"action": "de_esser", "target": "master"},
                  {"action": "dither", "target": "master"}], master=MASTER, project_path=PROJECT)
    assert [s["status"] for s in steps] == ["unsupported", "unsupported"]


def test_verified_writes_require_a_project_path():
    steps = plan([{"action": "eq.bell", "target": "master", "freq_hz": 300,
                   "gain_db": -2.0, "q": 4.0}], master=MASTER)
    assert steps[0]["status"] == "unresolved"
    assert "--project" in steps[0]["why"]


def test_unmapped_role_is_unresolved():
    steps = plan([{"action": "fader", "target": "drums", "gain_db": -2.0}], {})
    assert steps[0]["status"] == "unresolved"
    assert "no track" in steps[0]["why"]


def test_send_back_is_terminal():
    steps = plan([{"action": "send_back", "reasons": ["already_mastered:measured"]}])
    assert steps[0]["status"] == "terminal" and "calls" not in steps[0]


def test_summary_counts_calls():
    steps = plan([{"action": "limiter", "target": "master", "ceiling_dbtp": -1.0},
                  {"action": "de_esser", "target": "master"}],
                 master=MASTER, project_path=PROJECT)
    result = summary(steps)
    assert result["total"] == 2 and result["calls"] == 2


def test_unreachable_bridge_reports_an_error():
    from adapter.logic_adapter import apply
    steps = plan([{"action": "fader", "target": "vocal", "gain_db": -2.0}],
                 {"vocal": {"index": 1, "name": "Vox"}})
    results = apply(steps, endpoint="http://127.0.0.1:1/mcp", timeout=2)
    assert results and results[0]["status"] == "error"


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
