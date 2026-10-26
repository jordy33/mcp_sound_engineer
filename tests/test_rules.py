"""Domain tests: they assert the RULE's behaviour, not the code's arithmetic."""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import rules  # noqa: E402


def _mix_answers(**over):
    base = {k: {"type": v["type"], "noul": 0.1, "score": 0.0,
                "choice": next(iter(v.get("criteria", {"none": ""})))}
            for k, v in rules.MIX_QUESTIONS.items()}
    for k, v in over.items():
        base[k].update(v)
    return base


def _answers(**over):
    base = {k: {"type": v["type"],
                "noul": 0.1, "score": 0.0,
                "choice": next(iter(v.get("criteria", {"none": ""})))} for k, v in rules.MASTER_QUESTIONS.items()}
    for k, v in over.items():
        base[k].update(v)
    return base


def _master_facts(lufs: float = -18.0, tp: float = -1.0, **extra):
    base = {"loudness": {"integrated_lufs": lufs, "true_peak_dbtp": tp,
                         "loudness_range_lu": 9.0},
            "stereo": {"phase_correlation": 0.9}, "clipping": {"count": 0},
            "noise": {"floor_dbfs": None},
            "bandwidth": {"lossy_suspected": False, "drop_db": 0.0},
            "duration_seconds": 60, "sample_rate": 44100}
    base.update(extra)
    return base


def _limiter(recipe):
    return next(a for a in recipe if a.get("action") == "limiter")


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
    facts = _master_facts(-9.5, -0.2, loudness={"loudness_range_lu": 2.0,
                                               "true_peak_dbtp": -0.2,
                                               "integrated_lufs": -9.5},
                          duration_seconds=10, sample_rate=48000)
    recipe = rules.build_master_recipe(_answers(), facts, "spotify")
    assert len(recipe) == 1 and recipe[0]["action"] == "send_back"
    assert "already_mastered:measured" in recipe[0]["reasons"]


def test_a_few_overs_do_not_send_back():
    """500 frames or 0.01 % must BOTH be met, otherwise a 10 s file trips on 44."""
    facts = _master_facts(-18.0, -1.0, clipping={"count": 60},
                          duration_seconds=10, sample_rate=44100)
    gates = rules.evaluate_gates(facts)
    assert not gates["send_back"], gates


def test_a_handful_of_overs_in_a_short_file_does_not_send_back():
    """0.01 % of 1 s is 4.41 frames, but the 500-frame floor still has to be met."""
    facts = _master_facts(-18.0, -1.0, clipping={"count": 5},
                          duration_seconds=1, sample_rate=44100)
    assert not rules.evaluate_gates(facts)["send_back"]


def test_sustained_clipping_does_send_back():
    facts = _master_facts(-14.0, -0.5, clipping={"count": 5000},
                          duration_seconds=10, sample_rate=44100)
    assert rules.evaluate_gates(facts)["send_back"]


def test_noise_floor_gate():
    facts = _master_facts(-18.0, -1.0, noise={"floor_dbfs": -40.0},
                          duration_seconds=200)
    assert "noise_floor:measured" in rules.evaluate_gates(facts)["reasons"]


def test_lossy_source_gate():
    facts = _master_facts(-18.0, -1.0, bandwidth={"lossy_suspected": True,
                                                  "drop_db": 0.0},
                          duration_seconds=200)
    assert "lossy_source:measured" in rules.evaluate_gates(facts)["reasons"]


def test_missing_gate_inputs_are_unmeasured_not_healthy():
    """An absent measurement must not be defaulted to a safe-looking number."""
    facts = _master_facts()
    del facts["stereo"]["phase_correlation"]
    del facts["loudness"]["loudness_range_lu"]
    del facts["clipping"]["count"]
    del facts["bandwidth"]
    gates = rules.evaluate_gates(facts)
    assert gates["send_back"] is True, gates
    assert gates["complete"] is False, gates
    assert set(gates["unmeasured"]) == {
        "stereo.phase_correlation", "loudness.loudness_range_lu",
        "clipping.count", "bandwidth"}, gates
    assert "unmeasured:stereo.phase_correlation" in gates["reasons"]


def test_noise_floor_none_means_no_silence_not_unmeasured():
    facts = _master_facts(noise={"floor_dbfs": None})
    gates = rules.evaluate_gates(facts)
    assert gates["complete"] is True, gates
    assert "unmeasured:noise.floor_dbfs" not in gates["reasons"], gates


def test_unmeasured_gate_turns_the_recipe_into_a_terminal_send_back():
    facts = _master_facts()
    del facts["stereo"]["phase_correlation"]
    recipe = rules.build_master_recipe(_answers(), facts, "spotify")
    assert len(recipe) == 1 and recipe[0]["action"] == "send_back", recipe
    assert "unmeasured:stereo.phase_correlation" in recipe[0]["reasons"], recipe


def test_correlation_drop_is_not_invented_when_unmeasured():
    drop = rules.correlation_drop({"stereo": {}}, {"stereo": {"phase_correlation": 0.8}})
    assert drop["drop"] is None and drop["exceeds"] is False, drop
    assert drop["unmeasured"] == ["before"], drop


def test_mix_warns_when_no_stem_matches():
    warnings = rules.mix_warnings(_mix_answers(balance={"choice": "vocal_too_high"}),
                                  ["mixdown.wav"])
    assert warnings and "vocal" in warnings[0], warnings
    assert rules.mix_warnings(_mix_answers(balance={"choice": "vocal_too_high"}),
                              ["01_vocals.wav"]) == []


def test_unmastered_mix_is_not_sent_back():
    facts = _master_facts(-18.0, -1.0, stereo={"phase_correlation": 0.8},
                          clipping={"count": 3}, duration_seconds=200)
    assert not rules.evaluate_gates(facts)["send_back"]


# --- corrective EQ: over the cap is a send-back, never a clamp ---------------
def test_corrective_within_cap_is_applied():
    recipe = rules.build_master_recipe(
        _answers(low_end={"score": 3.0}, high_mid={"score": 3.0}),
        _master_facts(), "spotify")
    bells = [a for a in recipe if a["action"] == "eq.bell"]
    assert {a["measured_from"] for a in bells} == {"low_end", "high_mid"}
    for a in bells:
        assert abs(a["gain_db"]) <= rules.GATE["max_corrective_db_per_point"]
        assert 4.0 <= a["q"] <= 8.0, a


def test_corrective_beyond_cap_sends_back():
    recipe = rules.build_master_recipe(
        _answers(low_end={"score": 4.0}), _master_facts(), "spotify")
    assert recipe[0]["action"] == "send_back"
    assert any("corrective_beyond_cap" in r for r in recipe[0]["reasons"])


def test_cumulative_corrective_includes_the_sibilance_cut():
    answers = _answers(low_end={"score": 3.5}, high_mid={"score": 3.5},
                       sibilance={"choice": "cut"})
    recipe = rules.build_master_recipe(answers, _master_facts(), "spotify")
    assert recipe[0]["action"] == "send_back"
    assert "corrective_beyond_cap:cumulative:7.5dB" in recipe[0]["reasons"], recipe
    assert all(a["action"] != "eq.bell" for a in recipe), recipe


def test_sibilance_cut_alone_stays_within_the_q_range():
    recipe = rules.build_master_recipe(
        _answers(sibilance={"choice": "cut"}), _master_facts(), "spotify")
    bell = next(a for a in recipe if a["action"] == "eq.bell")
    assert bell["freq_hz"] == 7000 and 4.0 <= bell["q"] <= 8.0, bell
    assert bell["gain_db"] != 0.0, bell


# --- limiter: two constraints, arithmetic instead of model opinion ----------
def test_limiter_plan_case_1_loud_peak():
    """Hot master: no gain is added; the peak alone needs 2 dB of reduction."""
    plan = rules.plan_limiter(-10.0, 1.0, -1.0, desired_lufs=-14.0)
    assert plan["input_gain_db"] == 0.0, plan
    assert plan["expected_gain_reduction_db"] == 2.0, plan
    assert plan["loudness_reached"] is True, plan
    assert plan["approx_lufs_after"] == -10.0, plan
    assert plan["plr_db"] == 11.0, plan


def test_limiter_plan_case_2_loudness_not_reached():
    """The peak cap stops the loudness request at 4 dB; the result is -16, not -14."""
    plan = rules.plan_limiter(-20.0, -1.0, -1.0, desired_lufs=-14.0)
    assert plan["input_gain_db"] == 4.0, plan
    assert plan["expected_gain_reduction_db"] == 4.0, plan
    assert plan["loudness_reached"] is False, plan
    assert plan["approx_lufs_after"] == -16.0, plan
    assert plan["plr_db"] == 19.0, plan


def test_limiter_plan_case_3_small_move():
    """A 0.5 dB loudness move only needs 0.5 dB of limiting."""
    plan = rules.plan_limiter(-14.5, -1.0, -1.0, desired_lufs=-14.0)
    assert plan["input_gain_db"] == 0.5, plan
    assert plan["expected_gain_reduction_db"] == 0.5, plan
    assert plan["loudness_reached"] is True, plan
    assert plan["approx_lufs_after"] == -14.0, plan


def test_limiter_default_is_peak_only():
    plan = rules.plan_limiter(-12.0, -0.5, -1.0)
    assert plan["input_gain_db"] == 0.0, plan
    assert plan["expected_gain_reduction_db"] == 0.5, plan
    assert plan["loudness_reached"] is True, plan


def test_limiter_never_lowers_for_loudness():
    plan = rules.plan_limiter(-10.0, -0.5, -1.0, desired_lufs=-14.0)
    assert plan["input_gain_db"] == 0.0, plan
    assert plan["expected_gain_reduction_db"] == 0.5, plan   # the peak still limits


def test_limiter_no_limiting_reports_the_real_projection():
    quiet = rules.plan_limiter(-20.0, -10.0, -1.0)
    assert (quiet["input_gain_db"], quiet["expected_gain_reduction_db"]) == (0.0, 0.0)
    assert quiet["approx_lufs_after"] == -20.0, quiet

    asked = rules.plan_limiter(-20.0, -10.0, -1.0, desired_lufs=-14.0)
    assert (asked["input_gain_db"], asked["expected_gain_reduction_db"]) == (6.0, 0.0)
    assert asked["approx_lufs_after"] == -14.0, asked
    assert asked["loudness_reached"] is True, asked


def test_limiter_peak_at_or_below_ceiling_invents_no_gain():
    at = rules.plan_limiter(-20.0, -1.0, -1.0)
    below = rules.plan_limiter(-24.0, -2.0, -1.0)
    for plan in (at, below):
        assert plan["input_gain_db"] == 0.0, plan
        assert plan["expected_gain_reduction_db"] == 0.0, plan


def test_limiter_loudness_intent_capped_by_peak():
    """6 dB asked; the peak budget allows 3.5 dB of input gain, not 4.5."""
    plan = rules.plan_limiter(-18.0, -0.5, -1.0, desired_lufs=-12.0)
    assert plan["input_gain_db"] == 3.5, plan
    assert plan["expected_gain_reduction_db"] == 4.0, plan
    assert plan["loudness_reached"] is False, plan
    assert plan["approx_lufs_after"] == -14.5, plan
    assert plan["plr_db"] == 17.5, plan


def test_limiter_action_carries_the_contract_fields():
    lim = _limiter(rules.build_master_recipe(_answers(), _master_facts(-18.0, -6.0),
                                             "spotify", desired_lufs=-14.0))
    assert lim["ceiling_dbtp"] == -1.0, lim
    assert lim["input_gain_db"] == 4.0, lim
    assert lim["expected_gain_reduction_db"] == 0.0, lim
    assert lim["desired_lufs"] == -14.0, lim
    assert lim["loudness_reached"] is True, lim
    assert lim["approx_lufs_after"] == -14.0, lim
    assert lim["plr_db"] == 12.0, lim
    assert "target_lufs" not in lim


def test_missing_measurements_send_back_instead_of_inventing_a_limiter():
    for facts in ({}, {"spectral": {"bands_db_rel": {}}},
                  {"loudness": {"integrated_lufs": -18.0}}):
        recipe = rules.build_master_recipe(_answers(), facts, "spotify")
        assert len(recipe) == 1 and recipe[0]["action"] == "send_back", (facts, recipe)
        assert any(r.startswith("unmeasured:") for r in recipe[0]["reasons"]), recipe


def test_neutral_limiter_plan_is_the_fallback_for_missing_loudness():
    plan = rules._limiter_plan({}, rules.PLATFORMS["spotify"], None)
    assert plan["input_gain_db"] == 0.0, plan
    assert plan["expected_gain_reduction_db"] == 0.0, plan
    assert plan["loudness_reached"] is True, plan
    assert plan["approx_lufs_after"] is None and plan["plr_db"] is None, plan


def test_limiter_trim_is_emitted():
    recipe = rules.build_master_recipe(_answers(), _master_facts(-10.0, 5.0), "spotify")
    lim = _limiter(recipe)
    assert lim["input_gain_db"] == -2.0, lim
    assert lim["expected_gain_reduction_db"] == 4.0, lim
    warnings = rules.limiter_warnings(recipe)
    assert any("input trim" in w for w in warnings), warnings
    assert not any("not reached" in w for w in warnings), warnings


def test_limiter_absurd_peak_sends_back():
    recipe = rules.build_master_recipe(_answers(), _master_facts(-10.0, 7.0), "spotify")
    assert recipe[0]["action"] == "send_back", recipe
    assert any("peak_beyond_limiter" in r for r in recipe[0]["reasons"]), recipe[0]


def test_limiter_no_name_error_with_mono_or_noise_facts():
    facts = _master_facts(-18.0, -1.0,
                          stereo={"phase_correlation": 0.9, "mono_cancel_db": -7.0},
                          noise={"floor_dbfs": -55.0})
    lim = _limiter(rules.build_master_recipe(_answers(), facts, "spotify"))
    assert lim["ceiling_dbtp"] == -1.0, lim


def test_dither_only_for_fixed_bit_depth_destinations():
    spotify = rules.build_master_recipe(_answers(), _master_facts(), "spotify")
    cd = rules.build_master_recipe(_answers(), _master_facts(), "cd")
    assert all(a["action"] != "dither" for a in spotify), spotify
    assert cd[-1]["action"] == "dither" and cd[-1]["bit_depth"] == 16


def test_streaming_platform_never_emits_dither_even_from_16_bit_source():
    facts = _master_facts(bit_depth=16)
    for platform in ("spotify", "apple_music", "youtube", "amazon", "tidal"):
        recipe = rules.build_master_recipe(_answers(), facts, platform)
        assert all(a["action"] != "dither" for a in recipe), (platform, recipe)


def test_platform_dither_type_wins():
    cd = rules.build_master_recipe(_answers(), _master_facts(), "cd")
    assert cd[-1]["type"] == rules.PLATFORMS["cd"]["dither"]


# --- ordering ---------------------------------------------------------------
def test_compressor_precedes_de_esser():
    answers = _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 2.0},
                       sibilance={"choice": "deess"})
    order = [a["action"] for a in rules.build_master_recipe(
        answers, _master_facts(), "spotify")]
    assert order.index("compressor") < order.index("de_esser"), order


def test_compressor_targets_the_master_in_a_mastering_recipe():
    answers = _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 2.0})
    comp = next(a for a in rules.build_master_recipe(
        answers, _master_facts(), "spotify") if a["action"] == "compressor")
    assert comp["target"] == "master", comp


def test_gain_reduction_respects_the_source_range():
    answers = _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 0.0})
    comp = next(a for a in rules.build_master_recipe(
        answers, _master_facts(), "spotify") if a["action"] == "compressor")
    assert comp["gain_reduction_db"] >= rules.GATE["min_gain_reduction_db"]


def test_abstract_scores_map_explicitly_onto_the_gain_reduction_range():
    low, high = rules.GATE["min_gain_reduction_db"], rules.GATE["max_gain_reduction_db"]
    assert rules.map_score(2.0, low, high) == low
    assert rules.map_score(6.0, low, high) == 2.0
    assert rules.map_score(10.0, low, high) == high
    assert rules.map_score(0.0, low, high) == low       # below the scale
    assert rules.map_score(12.0, low, high) == high     # saturated
    for score, expected in ((2.0, 1.0), (4.0, 1.5), (6.0, 2.0),
                            (8.0, 2.5), (10.0, 3.0)):
        comp = next(a for a in rules.build_master_recipe(
            _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": score}),
            _master_facts(), "spotify") if a["action"] == "compressor")
        assert comp["gain_reduction_db"] == expected, (score, comp)


# --- mix half ---------------------------------------------------------------
def test_every_balance_label_lowers_the_culprit():
    for label in ("vocal_too_high", "drums_too_loud", "bass_too_heavy"):
        answers = _mix_answers(balance={"choice": label})
        recipe = rules.build_mix_recipe(answers, ["01_vocals.wav", "02_drums.wav", "03_bass.wav"])
        faders = [a for a in recipe if a["action"] == "fader"]
        assert faders and all(a["gain_db"] < 0 for a in faders), (label, recipe)


def test_fader_points_at_a_real_stem():
    recipe = rules.build_mix_recipe(_mix_answers(balance={"choice": "vocal_too_high"}),
                                    ["01_vocals.wav", "02_drums.wav"])
    assert recipe and recipe[0]["stem"] == "01_vocals.wav", recipe


def test_no_stem_match_means_no_invented_fader():
    assert rules.build_mix_recipe(_mix_answers(balance={"choice": "vocal_too_high"}),
                                  ["mixdown.wav"]) == []


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


def test_validate_recipe_catches_cap_violations():
    bad = [
        {"action": "eq.bell", "target": "master", "schema": rules.RECIPE_SCHEMA_VERSION,
         "freq_hz": 300, "gain_db": -5.0, "role": "corrective"},
        {"action": "compressor", "target": "master", "schema": rules.RECIPE_SCHEMA_VERSION,
         "gain_reduction_db": 9.0},
        {"action": "limiter", "target": "master", "schema": rules.RECIPE_SCHEMA_VERSION,
         "ceiling_dbtp": -1.0, "expected_gain_reduction_db": 8.0},
    ]
    codes = {v["code"] for v in rules.validate_recipe(bad)}
    assert {"corrective_cap", "gain_reduction_cap", "limiter_cap"} <= codes, codes


def test_validate_recipe_rejects_send_back_not_terminal():
    recipe = [
        {"action": "send_back", "target": "master", "schema": rules.RECIPE_SCHEMA_VERSION,
         "reasons": ["already_mastered:measured"]},
        {"action": "limiter", "target": "master", "schema": rules.RECIPE_SCHEMA_VERSION},
    ]
    assert "send_back_not_terminal" in {v["code"] for v in rules.validate_recipe(recipe)}


def test_validate_recipe_rejects_unknown_action_and_schema():
    recipe = [{"action": "eq.teleport", "target": "master", "schema": "1.0.0"}]
    codes = {v["code"] for v in rules.validate_recipe(recipe)}
    assert "unknown_action" in codes


def test_validate_recipe_accepts_a_good_recipe():
    good = rules.build_master_recipe(
        _answers(), _master_facts(-18.0, -1.0, duration_seconds=200), "spotify")
    assert rules.validate_recipe(good) == [], good


def test_validate_recipe_requires_limiter_control_fields():
    recipe = [{"action": "limiter", "target": "master",
               "schema": rules.RECIPE_SCHEMA_VERSION}]
    codes = {v["code"] for v in rules.validate_recipe(recipe)}
    assert {"missing_limiter_ceiling", "missing_limiter_input_gain",
            "missing_limiter_gr"} <= codes, codes


def test_validate_recipe_rejects_absurd_input_trim():
    recipe = [{"action": "limiter", "target": "master",
               "schema": rules.RECIPE_SCHEMA_VERSION, "ceiling_dbtp": -1.0,
               "input_gain_db": -4.0, "expected_gain_reduction_db": 0.0}]
    assert "limiter_input_trim_cap" in {v["code"] for v in rules.validate_recipe(recipe)}


def test_validate_recipe_rejects_ceiling_above_minus_one():
    recipe = [{"action": "limiter", "target": "master",
               "schema": rules.RECIPE_SCHEMA_VERSION, "ceiling_dbtp": 0.5,
               "input_gain_db": 0.0, "expected_gain_reduction_db": 0.0}]
    assert "limiter_ceiling" in {v["code"] for v in rules.validate_recipe(recipe)}


def test_validate_recipe_rejects_non_numeric_limiter_fields():
    recipe = [{"action": "limiter", "target": "master",
               "schema": rules.RECIPE_SCHEMA_VERSION, "ceiling_dbtp": "x",
               "input_gain_db": None, "expected_gain_reduction_db": []}]
    violations = rules.validate_recipe(recipe)          # must not raise
    invalid = [v for v in violations if v["code"] == "invalid_limiter_field"]
    assert len(invalid) == 3, violations


def test_no_duplicate_top_level_definitions():
    for rel in ("server/rules.py", "server/engineer_server.py", "adapter/logic_adapter.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = [node.name for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert not duplicates, (rel, duplicates)


def test_limiter_warning_for_unreached_loudness():
    recipe = [{"action": "limiter", "target": "master",
               "schema": rules.RECIPE_SCHEMA_VERSION, "ceiling_dbtp": -1.0,
               "input_gain_db": 4.0, "expected_gain_reduction_db": 4.0,
               "desired_lufs": -14.0, "loudness_reached": False,
               "approx_lufs_after": -16.0, "plr_db": 19.0}]
    warnings = rules.limiter_warnings(recipe)
    assert any("not reached" in w and "PLR" in w for w in warnings), warnings


def test_master_questions_have_no_limiter_question():
    assert all("limiter" not in key.lower() for key in rules.MASTER_QUESTIONS)


def test_noise_floor_minor_is_implemented():
    warnings = rules.loudness_warnings({"noise": {"floor_dbfs": -55.0}},
                                       rules.PLATFORMS["spotify"])
    assert any("minor but audible" in w for w in warnings), warnings
    assert "noise_floor_minor_db" in rules.IMPLEMENTED_GATES
    assert "noise_floor_minor_db" not in rules.UNIMPLEMENTED_GATES


def test_lossy_gate_uses_the_gate_constant():
    over = _master_facts(bandwidth={"drop_db": rules.GATE["lossy_shelf_drop_db"] + 0.5,
                                    "lossy_suspected": False})
    at = _master_facts(bandwidth={"drop_db": rules.GATE["lossy_shelf_drop_db"],
                                  "lossy_suspected": False})
    assert "lossy_source:measured" in rules.evaluate_gates(over)["reasons"]
    assert "lossy_source:measured" not in rules.evaluate_gates(at)["reasons"]


def test_correlation_drop_detects_image_loss():
    before = {"stereo": {"phase_correlation": 0.9}}
    after = {"stereo": {"phase_correlation": 0.6}}
    assert rules.correlation_drop(before, after)["exceeds"] is True
    assert rules.correlation_drop(before, {"stereo": {"phase_correlation": 0.85}})["exceeds"] is False


def test_stem_matching_uses_basename_not_path():
    path = "/Bounces/Vocals_Session/02_drums.wav"
    assert rules._match_stem([path], "drums") == path
    assert rules._match_stem([path], "vocal") is None
    assert rules._match_stem(["Lead Guitar.wav"], "vocal") is None
    assert rules._match_stem(["01_Kick.wav"], "drums") == "01_Kick.wav"


def test_require_rejects_missing_field():
    answers = _answers()
    del answers["low_end"]["score"]
    try:
        rules.build_master_recipe(answers, {}, "spotify")
    except ValueError as exc:
        assert "low_end.score" in str(exc), exc
    else:
        raise AssertionError("a score answer without 'score' must not default to 0")


def test_gain_reduction_budget_uses_peak_gr():
    """The limiter's 4 dB come from the peak, not from -8 vs the -14 playback figure."""
    facts = _master_facts(-8.0, 3.0)
    recipe = rules.build_master_recipe(
        _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 10.0}), facts, "spotify")
    assert recipe[0]["action"] == "send_back", recipe
    assert any("gain_reduction_budget" in r for r in recipe[0]["reasons"]), recipe[0]


def test_limiter_budget_with_desired_lufs():
    """A loudness request reaches the peak cap: 4 dB limiter GR + 3 dB glue = 7."""
    facts = _master_facts(-8.0, -0.5)
    recipe = rules.build_master_recipe(
        _answers(dynamics={"choice": "glue"}, dynamics_amount={"score": 10.0}),
        facts, "spotify", desired_lufs=-2.0)
    assert recipe[0]["action"] == "send_back", recipe
    assert any("gain_reduction_budget" in r for r in recipe[0]["reasons"]), recipe[0]


def test_dither_skipped_when_bits_are_not_reduced():
    already_16 = _master_facts(bit_depth=16)
    assert all(a["action"] != "dither"
               for a in rules.build_master_recipe(_answers(), already_16, "cd"))
    assert rules.build_master_recipe(
        _answers(), _master_facts(bit_depth=24), "cd")[-1]["action"] == "dither"


def test_corrective_uses_the_measured_band():
    facts = _master_facts(spectral={
        "bands_db_rel": {"sub": -50.0, "bass": 0.0, "low_mid": -45.0,
                         "mid": -40.0, "high_mid": -45.0, "high": -50.0}})
    recipe = rules.build_master_recipe(_answers(low_end={"score": 3.0}), facts, "spotify")
    bell = next(a for a in recipe if a["action"] == "eq.bell")
    assert bell["freq_hz"] == rules.BAND_CENTERS["bass"], bell
    assert bell["band"] == "bass"


def test_corrective_uses_the_measured_reference_curve():
    master = {"sub": -40.0, "bass": -5.0, "low_mid": -7.0,
              "mid": -40.0, "high_mid": -40.0, "high": -40.0}
    measured = {"sub": -20.0, "bass": -2.0, "low_mid": -13.0,
                "mid": -10.0, "high_mid": -10.0, "high": -20.0}
    facts = _master_facts(spectral={"bands_db_rel": master})
    answers = _answers(low_end={"score": 3.0})

    default_bell = next(a for a in rules.build_master_recipe(answers, facts, "spotify")
                        if a["action"] == "eq.bell")
    measured_bell = next(a for a in rules.build_master_recipe(
        answers, facts, "spotify", reference_curve=measured) if a["action"] == "eq.bell")
    assert default_bell["band"] == "bass", default_bell
    assert measured_bell["band"] == "low_mid", measured_bell
    assert measured_bell["freq_hz"] == rules.BAND_CENTERS["low_mid"], measured_bell


def test_partial_reference_curve_fails_loud():
    partial = dict(rules.REFERENCE_CURVE)
    del partial["bass"]
    for bad in ({}, partial):
        try:
            rules.build_master_recipe(_answers(), {}, "spotify", reference_curve=bad)
        except ValueError as exc:
            assert "bass" in str(exc), exc
        else:
            raise AssertionError("a partial reference curve must not be silently completed")


def test_mono_compatibility_warning():
    facts = {"stereo": {"mono_cancel_db": -30.0}, "loudness": {"integrated_lufs": -14.0,
                                                               "true_peak_dbtp": -1.2}}
    warnings = rules.loudness_warnings(facts, rules.PLATFORMS["spotify"])
    assert any("mono fold-down" in w for w in warnings), warnings


def test_mix_masking_clamp_is_reported_not_hidden():
    answers = _mix_answers(masking={"score": 10.0})
    stems = ["01_vocals.wav"]
    recipe = rules.build_mix_recipe(answers, stems)
    bell = next(a for a in recipe if a["action"] == "eq.bell")
    assert bell["gain_db"] == -rules.GATE["max_corrective_db_per_point"], bell
    warnings = rules.mix_warnings(answers, stems)
    assert any("capped" in w and "structural" in w for w in warnings), warnings


def test_mix_masking_within_cap_is_not_warned():
    answers = _mix_answers(masking={"score": 3.5})
    assert not any("capped" in w for w in rules.mix_warnings(answers, ["01_vocals.wav"]))


def test_gain_staging_clamp_is_reported_and_unmeasured_is_named():
    stems = [{"path": "/s/01_vocals.wav", "integrated_lufs": -30.0},
             {"path": "/s/02_bass.wav"}]
    warnings = rules.mix_warnings(_mix_answers(), [s["path"] for s in stems], stems)
    assert any("01_vocals.wav" in w and "capped" in w for w in warnings), warnings
    assert any("02_bass.wav" in w and "not measured" in w for w in warnings), warnings


def test_gain_staging_moves_each_stem_toward_minus_18():
    stems = [{"path": "/s/01_vocals.wav", "integrated_lufs": -12.0, "low_correlation": 1.0,
              "low_instrument": False, "phase_correlation": 0.9},
             {"path": "/s/02_bass.wav", "integrated_lufs": -28.0, "low_correlation": 1.0,
              "low_instrument": True, "phase_correlation": 0.9}]
    recipe = rules.build_mix_recipe(_mix_answers(), [s["path"] for s in stems], stems)
    moves = {a["stem"]: a["gain_db"] for a in recipe if a.get("reason") == "gain_staging"}
    assert moves["/s/01_vocals.wav"] == -6.0, moves
    assert moves["/s/02_bass.wav"] == 6.0, moves


def test_polarity_comes_before_eq_for_a_cancelling_stem():
    stems = [{"path": "/s/01_vocals.wav", "integrated_lufs": -18.0, "low_correlation": 1.0,
              "low_instrument": False, "phase_correlation": -0.4}]
    recipe = rules.build_mix_recipe(
        _mix_answers(phase_before_eq={"choice": "polarity"}, hpf_policy={"choice": "all_but_kick_bass"}),
        [s["path"] for s in stems], stems)
    order = [a["action"] for a in recipe]
    assert "polarity.invert" in order and "eq.high_pass_stem" in order, order
    assert order.index("polarity.invert") < order.index("eq.high_pass_stem"), order


def test_hpf_skips_low_instruments():
    stems = [{"path": "/s/01_vocals.wav", "integrated_lufs": -18.0, "low_correlation": 1.0,
              "low_instrument": False, "phase_correlation": 0.9},
             {"path": "/s/02_bass.wav", "integrated_lufs": -18.0, "low_correlation": 1.0,
              "low_instrument": True, "phase_correlation": 0.9}]
    recipe = rules.build_mix_recipe(_mix_answers(hpf_policy={"choice": "all_but_kick_bass"}),
                                    [s["path"] for s in stems], stems)
    hpf = [a for a in recipe if a["action"] == "eq.high_pass_stem"]
    assert [a["stem"] for a in hpf] == ["/s/01_vocals.wav"], hpf


def test_confidence_report_flags_coin_flips():
    report = rules.confidence_report({"a": {"score": 1.0, "confidence": 0.2},
                                      "b": {"score": 2.0, "confidence": 0.9}})
    assert report["needs_human"] is True and report["low"] == {"a": 0.2}
    assert rules.confidence_report({"a": {"score": 1.0, "confidence": 0.8}})["needs_human"] is False


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
