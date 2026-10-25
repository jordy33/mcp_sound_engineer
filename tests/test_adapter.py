"""Adapter tests: the plan must be honest about what Logic can and cannot do."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))

import rules  # noqa: E402
from adapter.logic_adapter import (CAPABILITIES, db_to_fader, plan,  # noqa: E402
                                   summary)


def test_every_action_has_a_capability():
    missing = [a for a in rules.ACTIONS if a not in CAPABILITIES]
    assert not missing, missing


def test_db_to_fader_is_monotonic_and_clamped():
    assert db_to_fader(0) == 1.0
    assert abs(db_to_fader(-6) - 0.5012) < 0.001
    assert abs(db_to_fader(-2) - 0.7943) < 0.001
    assert db_to_fader(6) == 1.0          # a boost cannot be expressed as 0..1
    assert db_to_fader(-60) >= 0.0


def test_fader_plan_targets_a_track():
    steps = plan([{"action": "fader", "target": "vocal", "gain_db": -2.0}], {"vocal": 3})
    assert steps[0]["status"] == "ready"
    assert steps[0]["calls"] == [{"tool": "logic_mixer",
                                  "arguments": {"command": "set_volume",
                                                "params": {"track": 3, "value": 0.7943}}}]


def test_unmapped_role_is_unresolved_not_guessed():
    steps = plan([{"action": "fader", "target": "drums", "gain_db": -2.0}], {})
    assert steps[0]["status"] == "unresolved"
    assert "no track index" in steps[0]["why"]


def test_eq_needs_calibration():
    steps = plan([{"action": "eq.bell", "target": "master", "freq_hz": 300, "gain_db": -2.0}],
                 master_track=0)
    assert steps[0]["status"] == "needs_calibration"
    assert "AX" in steps[0]["why"]


def test_limiter_and_dither_are_reported_unsupported():
    steps = plan([{"action": "limiter", "target": "master"},
                  {"action": "dither", "target": "master", "bit_depth": 16}])
    assert [s["status"] for s in steps] == ["unsupported", "unsupported"]
    assert all(s["why"] for s in steps)


def test_send_back_is_terminal_and_carries_no_tool():
    steps = plan([{"action": "send_back", "reasons": ["already_mastered:measured"]}])
    assert steps[0]["status"] == "terminal"
    assert "tool" not in steps[0]


def test_summary_counts_by_status():
    steps = plan([{"action": "fader", "target": "vocal", "gain_db": -2.0},
                  {"action": "limiter", "target": "master"}], {"vocal": 1})
    result = summary(steps)
    assert result["total"] == 2 and result["ready"] == 1
    assert result["by_status"]["unsupported"] == 1


def test_plan_never_mutates_the_recipe():
    recipe = [{"action": "fader", "target": "vocal", "gain_db": -2.0}]
    before = [dict(action) for action in recipe]
    plan(recipe, {"vocal": 1})
    assert recipe == before


def test_calibration_makes_eq_appliable():
    from adapter.calibration import Calibration
    cal = Calibration({"channel_eq": {"plugin_name": "Channel EQ",
                                      "param_index": {"freq": 0, "gain": 1, "q": 2},
                                      "curves": {"freq": [[20, 0.0], [20000, 1.0]],
                                                 "gain": [[-24, 0.0], [24, 1.0]],
                                                 "q": [[0.1, 0.0], [20.0, 1.0]]}}})
    steps = plan([{"action": "eq.bell", "target": "master", "freq_hz": 300,
                   "gain_db": -2.0, "q": 4.0}], master_track=0, calibration=cal)
    assert steps[0]["status"] == "ready"
    assert [c["arguments"]["command"] for c in steps[0]["calls"]] == [
        "insert_plugin", "set_plugin_param", "set_plugin_param", "set_plugin_param"]


def test_unreachable_bridge_reports_an_error_instead_of_raising():
    from adapter.logic_adapter import apply
    steps = plan([{"action": "fader", "target": "vocal", "gain_db": -2.0}], {"vocal": 1})
    results = apply(steps, endpoint="http://127.0.0.1:1/mcp", timeout=2)
    assert results and results[0]["status"] == "error", results


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
