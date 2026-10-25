"""Domain tests. These fail when a rule is wrong, not when a model is wrong."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import rules  # noqa: E402


def test_platform_schema():
    expected = {"normalization_lufs", "true_peak_ceiling", "bit_depth", "dither"}
    for name, spec in rules.PLATFORMS.items():
        assert set(spec) == expected, f"{name} has {sorted(spec)}"


def test_no_delivery_target_lufs():
    """Streaming loudness is a playback reference, never a delivery target."""
    for name, spec in rules.PLATFORMS.items():
        assert "lufs" not in spec and "target_lufs" not in spec, name


def test_physical_media_need_dither_and_16bit():
    assert rules.PLATFORMS["cd"]["bit_depth"] == 16
    assert rules.PLATFORMS["cd"]["dither"] == "tpdf"
    for streaming in ("spotify", "apple_music", "youtube", "tidal", "amazon"):
        assert rules.PLATFORMS[streaming]["bit_depth"] == 24
        assert rules.PLATFORMS[streaming]["dither"] == "none"


def test_streaming_ceilings():
    assert rules.PLATFORMS["spotify"]["true_peak_ceiling"] == -1.0
    assert rules.PLATFORMS["amazon"]["true_peak_ceiling"] == -2.0


def test_every_gate_is_classified():
    """No gate may be silently forgotten: it is either enforced or declared unimplemented."""
    classified = rules.IMPLEMENTED_GATES | rules.UNIMPLEMENTED_GATES
    assert classified == set(rules.GATE), classified ^ set(rules.GATE)
    assert not (rules.IMPLEMENTED_GATES & rules.UNIMPLEMENTED_GATES)


def _extreme_answers(level=4):
    return {
        "needs_corrective": {"type": "noul", "noul": 0.1},
        "low_end": {"type": "score", "score": level},
        "high_mid": {"type": "score", "score": level},
        "sibilance": {"type": "choice", "choice": "deess"},
        "dynamics": {"type": "choice", "choice": "glue"},
        "dynamics_amount": {"type": "score", "score": level},
        "limiter": {"type": "score", "score": level},
        "dither": {"type": "choice", "choice": "tpdf_16"},
    }


def test_corrective_eq_caps():
    recipe = rules.build_master_recipe(_extreme_answers(), {}, "spotify")
    bells = [a for a in recipe if a["action"] == "eq.bell"]
    assert bells, "extreme low end should request a corrective cut"
    for a in bells:
        assert abs(a["gain_db"]) <= rules.GATE["max_corrective_db_per_point"]
    assert sum(abs(a["gain_db"]) for a in bells) <= rules.GATE["max_cumulative_corrective_db"]


def test_gain_reduction_and_limiter_caps():
    recipe = rules.build_master_recipe(_extreme_answers(), {}, "spotify")
    comp = next(a for a in recipe if a["action"] == "compressor")
    lim = next(a for a in recipe if a["action"] == "limiter")
    assert comp["gain_reduction_db"] <= rules.GATE["max_gain_reduction_db"]
    assert lim["expected_gain_reduction_db"] <= rules.GATE["max_limiter_gr_db"]


def test_limiter_uses_delivery_ceiling_not_a_loudness_target():
    recipe = rules.build_master_recipe(_extreme_answers(), {}, "amazon")
    lim = next(a for a in recipe if a["action"] == "limiter")
    assert lim["ceiling_dbtp"] == rules.PLATFORMS["amazon"]["true_peak_ceiling"]
    assert "target_lufs" not in lim


def test_dither_is_last_and_matches_destination():
    for platform, spec in rules.PLATFORMS.items():
        recipe = rules.build_master_recipe({}, {}, platform)
        assert recipe[-1]["action"] == "dither"
        assert recipe[-1]["bit_depth"] in (16, 24)
        if spec["dither"] == "tpdf":
            assert recipe[-1]["bit_depth"] == 16


def test_send_back_is_terminal():
    facts = {"loudness": {"loudness_range_lu": 2.0, "true_peak_dbtp": -0.2, "integrated_lufs": -9.5},
             "stereo": {"phase_correlation": 0.9}, "clipping": {"count": 0},
             "duration_seconds": 10, "sample_rate": 48000}
    recipe = rules.build_master_recipe({}, facts, "spotify")
    assert len(recipe) == 1 and recipe[0]["action"] == "send_back"
    assert "already_mastered:measured" in recipe[0]["reasons"]


def test_unmastered_mix_is_not_sent_back():
    facts = {"loudness": {"loudness_range_lu": 11.0, "true_peak_dbtp": -1.0, "integrated_lufs": -18.0},
             "stereo": {"phase_correlation": 0.8}, "clipping": {"count": 3},
             "duration_seconds": 200, "sample_rate": 44100}
    recipe = rules.build_master_recipe({}, facts, "spotify")
    assert recipe[0]["action"] != "send_back"


def test_loudness_warning_fires_only_when_far_above_reference():
    target = rules.PLATFORMS["spotify"]
    hot = {"loudness": {"integrated_lufs": -8.0, "true_peak_dbtp": -0.2}}
    normal = {"loudness": {"integrated_lufs": -16.0, "true_peak_dbtp": -1.5}}
    assert rules.loudness_warnings(hot, target)
    assert rules.loudness_warnings(normal, target) == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ok  {name}")
            except Exception as exc:  # noqa: BLE001 — any failure is a failed assertion
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} fallo(s)")
    sys.exit(1 if failures else 0)
