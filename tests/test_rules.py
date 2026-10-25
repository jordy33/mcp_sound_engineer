"""Domain tests: they assert the RULE's behaviour, not the code's arithmetic."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import rules  # noqa: E402


def _answers(**over):
    base = {k: {"type": v["type"],
                "noul": 0.1, "score": 0.0,
                "choice": next(iter(v.get("criteria", {"none": ""})))} for k, v in rules.MASTER_QUESTIONS.items()}
    for k, v in over.items():
        base[k].update(v)
    return base


# --- destinations -----------------------------------------------------------
def test_platform_schema():
    expected = {"normalization_lufs", "true_peak_ceiling", "bit_depth", "dither"}
    for name, spec in rules.PLATFORMS.items():
        assert set(spec) == expected, f"{name}: {sorted(spec)}"


def test_no_delivery_target_lufs():
    for name, spec in rules.PLATFORMS.items():
        assert "lufs" not in spec and "target_lufs" not in spec, name


def test_unknown_platform_fails_loud():
    try:
        rules.loudness_target("napster")
    except ValueError as exc:
        assert "napster" in str(exc)
    else:
        raise AssertionError("must not fall back silently")


def test_cd_and_vinyl_ceilings():
    assert rules.PLATFORMS["cd"]["true_peak_ceiling"] == -1.0
    assert rules.PLATFORMS["vinyl"]["true_peak_ceiling"] == -3.0


def test_reference_curve_partitions_energy():
    total = sum(10 ** (v / 10) for v in rules.REFERENCE_CURVE.values())
    assert abs(10 * __import__("math").log10(total)) < 0.1, total


# --- gates ------------------------------------------------------------------
def test_every_gate_is_classified():
    assert rules.IMPLEMENTED_GATES | rules.UNIMPLEMENTED_GATES == set(rules.GATE)
    assert not (rules.IMPLEMENTED_GATES & rules.UNIMPLEMENTED_GATES)


def test_send_back_is_terminal_and_attributed():
    facts = {"loudness": {"loudness_range_lu": 2.0, "true_peak_dbtp": -0.2, "integrated_lufs": -9.5},
             "stereo": {"phase_correlation": 0.9}, "clipping": {"count": 0},
             "duration_seconds": 10, "sample_rate": 48000}
    recipe = rules.build_master_recipe(_answers(), facts, "spotify")
    assert len(recipe) == 1 and recipe[0]["action"] == "send_back"
    assert "already_mastered:measured" in recipe[0]["reasons"]


def test_a_few_overs_do_not_send_back():
    """500 samples or 0.01 % must BOTH be met, otherwise a 10 s file trips on 44."""
    facts = {"loudness": {"loudness_range_lu": 11.0, "true_peak_dbtp": -1.0, "integrated_lufs": -18.0},
             "stereo": {"phase_correlation": 0.8}, "clipping": {"count": 60},
             "duration_seconds": 10, "sample_rate": 44100}
    gates = rules.evaluate_gates(facts)
    assert not gates["send_back"], gates


def test_sustained_clipping_does_send_back():
    facts = {"loudness": {"loudness_range_lu": 11.0, "true_peak_dbtp": -0.5, "integrated_lufs": -14.0},
             "stereo": {"phase_correlation": 0.8}, "clipping": {"count": 5000},
             "duration_seconds": 10, "sample_rate": 44100}
    assert rules.evaluate_gates(facts)["send_back"]


def test_noise_floor_gate():
    facts = {"loudness": {"loudness_range_lu": 11.0, "true_peak_dbtp": -1.0, "integrated_lufs": -18.0},
             "stereo": {"phase_correlation": 0.9}, "clipping": {"count": 0},
             "noise": {"floor_dbfs": -40.0}, "duration_seconds": 200, "sample_rate": 44100}
    assert "noise_floor:measured" in rules.evaluate_gates(facts)["reasons"]


def test_lossy_source_gate():
    facts = {"loudness": {"loudness_range_lu": 11.0, "true_peak_dbtp": -1.0, "integrated_lufs": -18.0},
             "stereo": {"phase_correlation": 0.9}, "clipping": {"count": 0},
             "bandwidth": {"lossy_suspected": True}, "duration_seconds": 200, "sample_rate": 44100}
    assert "lossy_source:measured" in rules.evaluate_gates(facts)["reasons"]


def test_mix_warns_when_no_stem_matches():
    answers = {"balance": {"choice": "vocal_too_high"}, "masking": {"score": 0.0},
               "bus_comp": {"choice": "none"}}
    warnings = rules.mix_warnings(answers, ["mixdown.wav"])
    assert warnings and "vocal" in warnings[0], warnings
    assert rules.mix_warnings(answers, ["01_vocals.wav"]) == []


def test_unmastered_mix_is_not_sent_back():
    facts = {"loudness": {"loudness_range_lu": 11.0, "true_peak_dbtp": -1.0, "integrated_lufs": -18.0},
             "stereo": {"phase_correlation": 0.8}, "clipping": {"count": 3},
             "duration_seconds": 200, "sample_rate": 44100}
    assert not rules.evaluate_gates(facts)["send_back"]


# --- corrective EQ: over the cap is a send-back, never a clamp ---------------
def test_corrective_within_cap_is_applied():
    recipe = rules.build_master_recipe(_answers(low_end={"score": 3.0}, high_mid={"score": 3.0}),
                                       {}, "spotify")
    bells = [a for a in recipe if a["action"] == "eq.bell"]
    assert {a["measured_from"] for a in bells} == {"low_end", "high_mid"}
    for a in bells:
        assert abs(a["gain_db"]) <= rules.GATE["max_corrective_db_per_point"]


def test_corrective_beyond_cap_sends_back():
    recipe = rules.build_master_recipe(_answers(low_end={"score": 4.0}), {}, "spotify")
    assert recipe[0]["action"] == "send_back"
    assert any("corrective_beyond_cap" in r for r in recipe[0]["reasons"])


def test_cumulative_corrective_includes_the_sibilance_cut():
    answers = _answers(low_end={"score": 3.5}, high_mid={"score": 3.5},
                       sibilance={"choice": "cut"})
    recipe = rules.build_master_recipe(answers, {}, "spotify")
    assert recipe[0]["action"] == "send_back"
    assert any("cumulative" in r for r in recipe[0]["reasons"])


# --- arithmetic instead of model opinion ------------------------------------
def test_limiter_gr_is_computed_from_measured_lufs():
    facts = {"loudness": {"integrated_lufs": -12.0, "true_peak_dbtp": -0.5,
                          "loudness_range_lu": 9.0},
             "stereo": {"phase_correlation": 0.9}, "clipping": {"count": 0},
             "duration_seconds": 60, "sample_rate": 44100}
    lim = next(a for a in rules.build_master_recipe(_answers(), facts, "spotify")
               if a["action"] == "limiter")
    assert lim["expected_gain_reduction_db"] == 2.0, lim        # -12 vs -14 reference
    assert lim["ceiling_dbtp"] == rules.PLATFORMS["spotify"]["true_peak_ceiling"]
    assert "target_lufs" not in lim


def test_dither_only_for_fixed_bit_depth_destinations():
    spotify = rules.build_master_recipe(_answers(), {}, "spotify")
    cd = rules.build_master_recipe(_answers(), {}, "cd")
    assert all(a["action"] != "dither" for a in spotify), spotify
    assert cd[-1]["action"] == "dither" and cd[-1]["bit_depth"] == 16


# --- ordering ---------------------------------------------------------------
def test_compressor_precedes_de_esser():
    answers = _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 2.0},
                       sibilance={"choice": "deess"})
    order = [a["action"] for a in rules.build_master_recipe(answers, {}, "spotify")]
    assert order.index("compressor") < order.index("de_esser"), order


def test_compressor_targets_the_master_in_a_mastering_recipe():
    answers = _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 2.0})
    comp = next(a for a in rules.build_master_recipe(answers, {}, "spotify")
                if a["action"] == "compressor")
    assert comp["target"] == "master", comp


def test_gain_reduction_respects_the_source_range():
    answers = _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 0.0})
    comp = next(a for a in rules.build_master_recipe(answers, {}, "spotify")
                if a["action"] == "compressor")
    assert comp["gain_reduction_db"] >= rules.GATE["min_gain_reduction_db"]


# --- mix half ---------------------------------------------------------------
def test_every_balance_label_lowers_the_culprit():
    for label in ("vocal_too_high", "drums_too_loud", "bass_too_heavy"):
        answers = {"balance": {"choice": label}, "masking": {"score": 0.0},
                   "bus_comp": {"choice": "none"}}
        recipe = rules.build_mix_recipe(answers, ["vocals.wav", "drums.wav", "bass.wav"])
        faders = [a for a in recipe if a["action"] == "fader"]
        assert faders and all(a["gain_db"] < 0 for a in faders), (label, recipe)


def test_fader_points_at_a_real_stem():
    answers = {"balance": {"choice": "vocal_too_high"}, "masking": {"score": 0.0},
               "bus_comp": {"choice": "none"}}
    recipe = rules.build_mix_recipe(answers, ["01_vocals.wav", "02_drums.wav"])
    assert recipe and recipe[0]["stem"] == "01_vocals.wav", recipe


def test_no_stem_match_means_no_invented_fader():
    answers = {"balance": {"choice": "vocal_too_high"}, "masking": {"score": 0.0},
               "bus_comp": {"choice": "none"}}
    assert rules.build_mix_recipe(answers, ["mixdown.wav"]) == []


# --- contract ---------------------------------------------------------------
def test_recipe_never_reads_a_key_the_schema_lacks():
    """The test that would have caught low_end/low_mid and vocal_forward."""
    for question_set, builder in ((rules.MASTER_QUESTIONS, rules.build_master_recipe),
                                  (rules.MIX_QUESTIONS, rules.build_mix_recipe)):
        full = {k: {"type": v["type"], "noul": 0.1, "score": 0.0,
                    "choice": next(iter(v.get("criteria", {"none": ""})))} for k, v in question_set.items()}
        args = (full, {}) if builder is rules.build_master_recipe else (full, [])
        builder(*args)                      # must not raise
        for key in question_set:
            partial = {k: v for k, v in full.items() if k != key}
            args = (partial, {}) if builder is rules.build_master_recipe else (partial, [])
            try:
                builder(*args)
            except ValueError as exc:
                assert key in str(exc), exc
            else:
                raise AssertionError(f"{builder.__name__} tolerated missing {key!r}")


def test_action_rejects_unknown_target():
    try:
        rules._action("limiter", target="monitor")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown target must not be coerced to master")


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
