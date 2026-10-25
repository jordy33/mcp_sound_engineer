"""Professional mix/master rules and the DAW-neutral action vocabulary.

Every number here is a hard cap, not a suggestion. No DAW, plugin or vendor name
lives in this module: recipes are expressed in `action` primitives that any
adapter (a DAW bridge, a CLI, a test harness) can translate.

Sources: mastering-engineer methodology (fadelabs/phantom) + 2026 platform targets.
"""
from __future__ import annotations

RULES_VERSION = "1.0.0"
RECIPE_SCHEMA_VERSION = "1.1.0"

# --- Gates: when to refuse to master a mix -----------------------------------
GATE = {
    "max_corrective_db_per_point": 3.0,
    "max_cumulative_corrective_db": 6.0,
    "min_phase_correlation": 0.3,
    "noise_floor_moderate_db": -50.0,
    "noise_floor_minor_db": -60.0,
    "lossy_shelf_drop_db": 20.0,
    "max_limiter_gr_db": 4.0,
    "max_gain_reduction_db": 3.0,
}

# --- The ten stages, in order ------------------------------------------------
CHAIN = [
    ("eq.high_pass",   {"freq_hz": (20, 30), "slope_db_oct": (6, 12)}),
    ("eq.bell",        {"q": (4, 8), "max_gain_db": 3.0, "role": "corrective"}),
    ("denoise",        {"order": ("tonal", "declip", "broadband")}),
    ("compressor",     {"ratio": (1.5, 3.0), "attack_ms": (10, 30), "gr_db": 3.0}),
    ("dynamic_eq",     {"crossovers_hz": (80, 120, 2000, 3000, 8000, 10000)}),
    ("eq.shelf",       {"q": (0.5, 1.5), "role": "tonal"}),
    ("stereo.width",   {"mono_below_hz": 120, "max_width_db": 2.0}),
    ("limiter",        {"ceiling_dbtp": -1.0, "max_gr_db": 4.0}),
    ("loudness",       {"integrated_lufs": "platform"}),
    ("dither",         {"always_last": True}),
]

# --- Loudness targets per platform ------------------------------------------
# Streaming services NORMALIZE playback loudness. The numbers below are playback
# references, NOT delivery targets: one master with true peak under the ceiling serves
# all of them, and a quieter master is not a worse master. Only physical/broadcast
# destinations need a separate pass (bit depth, dither, ceiling).
PLATFORMS = {
    "spotify":     {"normalization_lufs": -14.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "apple_music": {"normalization_lufs": -16.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "youtube":     {"normalization_lufs": -14.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "amazon":      {"normalization_lufs": -14.0, "true_peak_ceiling": -2.0, "bit_depth": 24, "dither": "none"},
    "tidal":       {"normalization_lufs": -14.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "cd":          {"normalization_lufs": None,  "true_peak_ceiling": -0.1, "bit_depth": 16, "dither": "tpdf"},
    "podcast":     {"normalization_lufs": -16.0, "true_peak_ceiling": -1.0, "bit_depth": 16, "dither": "tpdf"},
}

# Which GATE constants are actually enforced by code (tests assert this set is complete).
IMPLEMENTED_GATES = {
    "max_corrective_db_per_point", "max_cumulative_corrective_db",
    "min_phase_correlation", "max_limiter_gr_db", "max_gain_reduction_db",
}
UNIMPLEMENTED_GATES = {"noise_floor_moderate_db", "noise_floor_minor_db", "lossy_shelf_drop_db"}

# --- Reference curve (dB relative to total energy), for deviation math ------
REFERENCE_CURVE = {
    "sub": -18.0, "bass": -11.0, "low_mid": -12.0,
    "mid": -8.0, "high_mid": -13.0, "high": -17.0,
}

# --- Neutral action vocabulary ---------------------------------------------
# An adapter maps `action` to its own surface. Examples of the mapping are the
# adapter's job, never ours: Channel EQ, ReaEQ, EQ3, a custom chain.
ACTIONS = {
    "eq.high_pass":  "high-pass filter",
    "eq.bell":       "parametric bell (corrective: q 4-8)",
    "eq.shelf":      "wide shelf (tonal shaping: q 0.5-1.5)",
    "compressor":    "broadband compressor",
    "dynamic_eq":    "frequency-selective dynamics",
    "de_esser":      "sibilance control",
    "stereo.width":  "stereo image (mono bass / width)",
    "limiter":       "peak limiter",
    "fader":         "channel gain",
    "pan":           "stereo position",
    "dither":        "output bit-depth conversion",
    "send_back":     "stop: the mix needs work before mastering",
}

TARGETS = {"master", "mix_bus", "vocal", "drums", "bass", "bus", "stem"}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def loudness_target(platform: str) -> dict:
    return PLATFORMS.get(platform.lower(), PLATFORMS["spotify"])


GATE_QUESTIONS = ("needs_corrective", "phase_problem", "clipped", "already_mastered")
GATE_THRESHOLD = 0.8          # model alone must be quite sure
MIN_CLIPPED_SAMPLES = 500     # a handful of overs is inaudible, not a send-back
MIN_CLIPPED_PROPORTION = 0.0001
MAX_LRA_FINISHED = 4.0        # LRA < 4 LU = already over-compressed


def evaluate_gates(answers: dict, facts: dict) -> dict:
    """Decide whether to refuse mastering, and say exactly why.

    Two layers: the model's calibrated answers, plus deterministic guards that do not
    depend on the model being right.
    """
    reasons: list[str] = []
    probabilities: dict[str, float] = {}
    for name in GATE_QUESTIONS:
        prob = float(answers.get(name, {}).get("noul", 0.0))
        probabilities[name] = round(prob, 2)
        if prob >= GATE_THRESHOLD:
            reasons.append(name)

    stereo = facts.get("stereo") or {}
    if float(stereo.get("phase_correlation", 1.0)) < GATE["min_phase_correlation"]:
        if "phase_problem" not in reasons:
            reasons.append("phase_problem:measured")

    loud = facts.get("loudness") or {}
    clipping = facts.get("clipping") or {}
    clipped_samples = int(clipping.get("count", 0))
    frames = float(facts.get("duration_seconds", 0)) * float(facts.get("sample_rate", 1)) or 1.0
    proportion = clipped_samples / frames
    if (clipped_samples >= MIN_CLIPPED_SAMPLES or proportion >= MIN_CLIPPED_PROPORTION) \
            and "clipped" not in reasons:
        reasons.append("clipped:measured")

    if (float(loud.get("loudness_range_lu", 99)) < MAX_LRA_FINISHED
            and float(loud.get("true_peak_dbtp", -99)) > -1.0
            and float(loud.get("integrated_lufs", -99)) > -12.0
            and "already_mastered" not in reasons):
        reasons.append("already_mastered:measured")

    return {"send_back": bool(reasons), "reasons": reasons, "probabilities": probabilities}


def _action(action: str, target: str = "master", **params) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    if target not in TARGETS:
        target = "master"
    return {"action": action, "target": target,
            "schema": RECIPE_SCHEMA_VERSION, **params}


def loudness_warnings(facts: dict, target: dict) -> list[str]:
    """Human-facing notes: what the platform will do to this master."""
    out: list[str] = []
    loud = facts.get("loudness") or {}
    lufs = float(loud.get("integrated_lufs", -99.0))
    tp = float(loud.get("true_peak_dbtp", -99.0))
    ceiling = target["true_peak_ceiling"]
    if tp > ceiling:
        out.append(f"true peak {tp} dBTP is above the {ceiling} dBTP ceiling for this destination")
    ref = target.get("normalization_lufs")
    if ref is not None and lufs > ref + 6:
        out.append(f"{round(lufs - ref, 1)} dB hotter than the {ref} LUFS playback reference — "
                   f"the service will turn it down, so those extra dB buy nothing and cost dynamics")
    return out


def build_master_recipe(answers: dict, facts: dict, platform: str = "spotify") -> list[dict]:
    """Typed answers -> DAW-neutral action plan, clipped by the craft's caps."""
    recipe: list[dict] = []
    target = loudness_target(platform)

    def noul(name: str, default: float = 0.0) -> float:
        return float(answers.get(name, {}).get("noul", default))

    def score(name: str, default: float = 0.0) -> float:
        return float(answers.get(name, {}).get("score", default))

    def choice(name: str, default: str = "") -> str:
        return str(answers.get(name, {}).get("choice", default))

    gates = evaluate_gates(answers, facts)
    if gates["send_back"]:
        return [_action("send_back", reasons=gates["reasons"],
                        probabilities=gates["probabilities"])]

    recipe.append(_action("eq.high_pass", freq_hz=25, slope_db_oct=12, reason="sub-bass cleanup"))

    total = 0.0
    for band, freq, q in (("low_mid", 300, 5.0), ("high_mid", 3000, 2.0)):
        level = score(band)
        if level >= 2.5:
            gain = -clamp((level - 2.0) * 1.5, 0.0, GATE["max_corrective_db_per_point"])
            gain = max(gain, -(GATE["max_cumulative_corrective_db"] - total))
            total += abs(gain)
            recipe.append(_action("eq.bell", freq_hz=freq, gain_db=round(gain, 1), q=q,
                                  role="corrective", measured_from=band))

    sib = choice("sibilance")
    if sib == "deess":
        recipe.append(_action("de_esser", freq_hz=6500, ratio=2.0))
    elif sib == "cut":
        recipe.append(_action("eq.bell", freq_hz=7000, gain_db=-1.5, q=6.0, role="corrective"))

    dyn = choice("dynamics")
    if dyn == "glue":
        recipe.append(_action("compressor", target="mix_bus", ratio=2.0, attack_ms=20, release_ms=200,
                              gain_reduction_db=clamp(0.5 + 0.5 * score("dynamics_amount"), 0.5,
                                                      GATE["max_gain_reduction_db"])))
    elif dyn == "expand":
        recipe.append(_action("compressor", target="mix_bus", mode="upward_expand",
                              reason="restore range on over-compressed material"))

    recipe.append(_action("limiter", ceiling_dbtp=target["true_peak_ceiling"],
                          expected_gain_reduction_db=round(clamp(score("limiter"), 0.0, GATE["max_limiter_gr_db"]), 1)))
    bit_depth = 16 if choice("dither") == "tpdf_16" else target["bit_depth"]
    recipe.append(_action("dither", bit_depth=bit_depth))
    return recipe


def build_mix_recipe(answers: dict, stems: list[str]) -> list[dict]:
    """Mix decisions: fader moves capped at +-3 dB, corrective EQ capped at 3 dB."""
    recipe: list[dict] = []
    balance = answers.get("balance", {}).get("choice")
    if balance and balance != "balanced":
        move = {"vocal_forward": ("vocal", 2.0), "drums_forward": ("drums", 2.0),
                "bass_heavy": ("bass", -2.0)}.get(balance)
        if move:
            recipe.append(_action("fader", target=move[0], gain_db=clamp(move[1], -3.0, 3.0)))

    masking = float(answers.get("masking", {}).get("score", 0.0))
    if masking >= 2.5:
        recipe.append(_action("eq.bell", target="bus", freq_hz=300,
                              gain_db=-clamp((masking - 2.0), 0.0, 3.0), q=4.0, role="corrective"))

    if answers.get("bus_comp", {}).get("choice") == "glue":
        recipe.append(_action("compressor", target="mix_bus", ratio=2.0, attack_ms=30,
                              release_ms=250, gain_reduction_db=2.0))
    return recipe
