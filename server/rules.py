"""Professional mix/master rules and the DAW-neutral action vocabulary.

Every number here is a hard cap, not a suggestion. No DAW, plugin or vendor name
lives in this module: recipes are expressed in `action` primitives that any
adapter can translate.

Sources (numbers):
  - mastering methodology: https://github.com/fadelabs/phantom/blob/main/plugin/skills/mastering-engineer/SKILL.md
  - platform loudness references: third-party cheat sheet, NOT verified against primary docs.
Product rules that are NOT in the source are marked "PRODUCT RULE" where they appear.
"""
from __future__ import annotations

import math

RULES_VERSION = "2.0.0"
RECIPE_SCHEMA_VERSION = "2.0.0"

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
    "min_gain_reduction_db": 1.0,
}

# Which GATE constants are actually enforced by code (tests assert completeness).
IMPLEMENTED_GATES = {
    "max_corrective_db_per_point", "max_cumulative_corrective_db",
    "min_phase_correlation", "max_limiter_gr_db", "max_gain_reduction_db",
    "min_gain_reduction_db",
}
UNIMPLEMENTED_GATES = {"noise_floor_moderate_db", "noise_floor_minor_db", "lossy_shelf_drop_db"}

# --- The ten stages, in order ------------------------------------------------
CHAIN = [
    ("eq.high_pass",   {"freq_hz": (20, 30), "slope_db_oct": (6, 12)}),
    ("eq.bell",        {"q": (4, 8), "max_gain_db": 3.0, "role": "corrective"}),
    ("denoise",        {"order": ("tonal", "declip", "broadband")}),
    ("compressor",     {"ratio": (1.5, 3.0), "attack_ms": (10, 30), "gr_db": (1.0, 3.0)}),
    ("de_esser",       {}),
    ("dynamic_eq",     {"crossovers_hz": (80, 120, 2000, 3000, 8000, 10000)}),
    ("eq.shelf",       {"q": (0.5, 1.5), "role": "tonal"}),
    ("stereo.width",   {"mono_below_hz": 120, "max_width_db": 2.0}),
    ("limiter",        {"ceiling_dbtp": -1.0, "max_gr_db": 4.0}),
    ("dither",         {"always_last": True}),
]

# --- Destinations -----------------------------------------------------------
# Streaming services NORMALIZE playback loudness. `normalization_lufs` is a playback
# reference, NOT a delivery target: one master under the ceiling serves all of them.
# `dither` is "none" unless the destination is a fixed 16-bit medium.
PLATFORMS = {
    "spotify":     {"normalization_lufs": -14.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "apple_music": {"normalization_lufs": -16.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "youtube":     {"normalization_lufs": -14.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "amazon":      {"normalization_lufs": -14.0, "true_peak_ceiling": -2.0, "bit_depth": 24, "dither": "none"},
    "tidal":       {"normalization_lufs": -14.0, "true_peak_ceiling": -1.0, "bit_depth": 24, "dither": "none"},
    "cd":          {"normalization_lufs": None,  "true_peak_ceiling": -1.0, "bit_depth": 16, "dither": "tpdf"},
    "vinyl":       {"normalization_lufs": None,  "true_peak_ceiling": -3.0, "bit_depth": 24, "dither": "none"},
    "podcast":     {"normalization_lufs": -16.0, "true_peak_ceiling": -1.0, "bit_depth": 16, "dither": "tpdf"},
}

# --- Reference curve (dB relative to total energy), normalized to sum to 0 dB --
_RAW_CURVE = {"sub": -18.0, "bass": -11.0, "low_mid": -12.0,
              "mid": -8.0, "high_mid": -13.0, "high": -17.0}
_TOTAL = sum(10 ** (v / 10) for v in _RAW_CURVE.values())
REFERENCE_CURVE = {k: round(10 * math.log10(10 ** (v / 10) / _TOTAL), 2) for k, v in _RAW_CURVE.items()}

# --- Neutral action vocabulary ----------------------------------------------
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

# --- Question schemas: the single source of truth for answer keys ------------
MASTER_QUESTIONS = {
    "low_end": {"type": "score",
                "instructions": "Low end vs reference_curve: how much low-mid/sub excess is there?",
                "criteria": ["well balanced", "slight excess", "noticeable", "heavy", "extreme"]},
    "high_mid": {"type": "score",
                 "instructions": "2-6 kHz vs reference_curve: how much presence excess is there?",
                 "criteria": ["none", "slight", "noticeable", "heavy", "extreme"]},
    "sibilance": {"type": "choice",
                  "instructions": "Which sibilance treatment fits, given the 5-9 kHz balance?",
                  "criteria": {"none": "no treatment needed",
                               "deess": "dynamic de-esser",
                               "cut": "narrow static cut around 7 kHz"}},
    "dynamics": {"type": "choice",
                 "instructions": "Dynamics work needed? LRA < 4 LU or crest < 6 dB means already compressed.",
                 "criteria": {"none": "already dense, do not compress",
                              "glue": "gentle bus glue, 1-3 dB GR",
                              "expand": "upward expansion to restore range"}},
    "dynamics_amount": {"type": "score",
                        "instructions": "If bus glue is used, how much gain reduction? 1-3 dB is the range.",
                        "criteria": ["0-1 dB", "1-1.5 dB", "1.5-2 dB", "2-2.5 dB", "2.5-3 dB"]},
}

MIX_QUESTIONS = {
    "balance": {"type": "choice",
                "instructions": "Which element is out of balance in the stems?",
                "criteria": {"balanced": "nothing is out of place",
                             "vocal_too_high": "vocal sits too high",
                             "drums_too_loud": "drums dominate",
                             "bass_too_heavy": "bass masks everything"}},
    "masking": {"type": "score",
                "instructions": "How much spectral masking is there between the stems?",
                "criteria": ["none", "slight", "noticeable", "heavy", "extreme"]},
    "bus_comp": {"type": "choice",
                 "instructions": "Does the mix bus need glue compression?",
                 "criteria": {"none": "no compression", "glue": "gentle bus glue"}},
}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def loudness_target(platform: str) -> dict:
    key = (platform or "").lower()
    if key not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}; choose one of {sorted(PLATFORMS)}")
    return PLATFORMS[key]


def require(answers: dict, questions: dict) -> None:
    """Fail loud when a recipe reads a key the schema never produces."""
    missing = [k for k in questions if k not in answers]
    if missing:
        raise ValueError(f"answers missing {missing}; schema defines {sorted(questions)}")


def _noul(answers, name, default=0.0):
    return float(answers.get(name, {}).get("noul", default))


def _score(answers, name, default=0.0):
    return float(answers.get(name, {}).get("score", default))


def _choice(answers, name, default=""):
    return str(answers.get(name, {}).get("choice", default))


def _action(action: str, target: str = "master", **params) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    if target not in TARGETS:
        raise ValueError(f"unknown target: {target}")
    return {"action": action, "target": target, "schema": RECIPE_SCHEMA_VERSION, **params}


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------
GATE_THRESHOLD = 0.8
MIN_CLIPPED_SAMPLES = 500
MIN_CLIPPED_PROPORTION = 0.0001
MAX_LRA_FINISHED = 4.0


def evaluate_gates(facts: dict) -> dict:
    """Refuse mastering, and say exactly why. Every condition here is measured.

    The decision model is deliberately NOT asked whether to refuse: a global yes/no
    duplicates the detailed scores and contradicts them when they disagree.
    """
    reasons: list[str] = []

    stereo = facts.get("stereo") or {}
    if float(stereo.get("phase_correlation", 1.0)) < GATE["min_phase_correlation"]:
        reasons.append("phase_problem:measured")

    loud = facts.get("loudness") or {}
    clipping = facts.get("clipping") or {}
    clipped = int(clipping.get("count", 0))
    frames = float(facts.get("duration_seconds", 0)) * float(facts.get("sample_rate", 1)) or 1.0
    if clipped >= MIN_CLIPPED_SAMPLES and (clipped / frames) >= MIN_CLIPPED_PROPORTION:
        reasons.append("clipped:measured")

    # PRODUCT RULE (not in the source): a finished master cannot be mastered again.
    if (float(loud.get("loudness_range_lu", 99)) < MAX_LRA_FINISHED
            and float(loud.get("true_peak_dbtp", -99)) > -1.0
            and float(loud.get("integrated_lufs", -99)) > -12.0):
        reasons.append("already_mastered:measured")

    return {"send_back": bool(reasons), "reasons": reasons}


def loudness_warnings(facts: dict, target: dict) -> list[str]:
    out: list[str] = []
    loud = facts.get("loudness") or {}
    lufs = float(loud.get("integrated_lufs", -99.0))
    tp = float(loud.get("true_peak_dbtp", -99.0))
    ceiling = target["true_peak_ceiling"]
    if tp > ceiling + 0.05:
        out.append(f"true peak {tp} dBTP is above the {ceiling} dBTP ceiling for this destination")
    ref = target.get("normalization_lufs")
    if ref is not None and lufs > ref + 6:
        out.append(f"{round(lufs - ref, 1)} dB hotter than the {ref} LUFS playback reference — "
                   f"the service will turn it down, so those extra dB buy nothing and cost dynamics")
    return out


def expected_limiter_gr(facts: dict, target: dict) -> float:
    """Arithmetic, not a model opinion: how far the master is above the playback reference."""
    ref = target.get("normalization_lufs")
    lufs = float((facts.get("loudness") or {}).get("integrated_lufs", ref or 0.0))
    if ref is None:
        return 0.0
    return round(clamp(lufs - ref, 0.0, GATE["max_limiter_gr_db"]), 1)


def build_master_recipe(answers: dict, facts: dict, platform: str = "spotify") -> list[dict]:
    require(answers, MASTER_QUESTIONS)
    target = loudness_target(platform)

    gates = evaluate_gates(facts)
    if gates["send_back"]:
        return [_action("send_back", reasons=gates["reasons"])]

    recipe: list[dict] = [_action("eq.high_pass", freq_hz=25, slope_db_oct=12, reason="sub-bass cleanup")]

    # Corrective moves: over the cap is a send-back, never a silent clamp.
    total = 0.0
    for band, freq in (("low_end", 300), ("high_mid", 3000)):
        level = _score(answers, band)
        if level < 2.5:
            continue
        needed = (level - 2.0) * 2.0   # level 4 (extreme) demands 4 dB -> send back
        if needed > GATE["max_corrective_db_per_point"]:
            return [_action("send_back",
                            reasons=[f"corrective_beyond_cap:{band}:{round(needed, 1)}dB"])]
        total += needed
        if total > GATE["max_cumulative_corrective_db"]:
            return [_action("send_back",
                            reasons=[f"corrective_beyond_cap:cumulative:{round(total, 1)}dB"])]
        recipe.append(_action("eq.bell", freq_hz=freq, gain_db=-round(needed, 1), q=4.0,
                              role="corrective", measured_from=band))

    # Dynamics before sibilance: the de-esser belongs to the dynamic stage, after compression.
    if _choice(answers, "dynamics") == "glue":
        gr = clamp(0.5 + 0.5 * _score(answers, "dynamics_amount"),
                   GATE["min_gain_reduction_db"], GATE["max_gain_reduction_db"])
        recipe.append(_action("compressor", target="master", ratio=2.0, attack_ms=20,
                              release_ms=200, gain_reduction_db=round(gr, 1)))
    elif _choice(answers, "dynamics") == "expand":
        recipe.append(_action("compressor", target="master", mode="upward_expand",
                              reason="restore range on over-compressed material"))

    sib = _choice(answers, "sibilance")
    if sib == "deess":
        recipe.append(_action("de_esser", freq_hz=6500, ratio=2.0))
    elif sib == "cut":
        needed = 1.5
        total += needed
        if total > GATE["max_cumulative_corrective_db"]:
            return [_action("send_back",
                            reasons=[f"corrective_beyond_cap:cumulative:{round(total, 1)}dB"])]
        recipe.append(_action("eq.bell", freq_hz=7000, gain_db=-needed, q=6.0, role="corrective"))

    recipe.append(_action("limiter", ceiling_dbtp=target["true_peak_ceiling"],
                          expected_gain_reduction_db=expected_limiter_gr(facts, target)))

    # Dither belongs to the destination, never to the model: only when reducing bits.
    if target["dither"] != "none":
        recipe.append(_action("dither", bit_depth=target["bit_depth"], type=target["dither"]))
    return recipe


def build_mix_recipe(answers: dict, stems: list[str]) -> list[dict]:
    """Mix decisions. PRODUCT DEFAULTS: not sourced from the mastering methodology."""
    require(answers, MIX_QUESTIONS)
    recipe: list[dict] = []

    balance = _choice(answers, "balance")
    # Every label names the problem, so every correction lowers the offender.
    moves = {"vocal_too_high": ("vocal", -2.0), "drums_too_loud": ("drums", -2.0),
             "bass_too_heavy": ("bass", -2.0)}
    if balance in moves:
        target, gain = moves[balance]
        stem = _match_stem(stems, target)
        if stem:
            recipe.append(_action("fader", target=target, gain_db=clamp(gain, -3.0, 3.0),
                                  stem=stem))
    masking = _score(answers, "masking")
    if masking >= 2.5:
        recipe.append(_action("eq.bell", target="bus", freq_hz=300,
                              gain_db=-clamp(masking - 2.0, 0.0, 3.0), q=4.0, role="corrective"))
    if _choice(answers, "bus_comp") == "glue":
        recipe.append(_action("compressor", target="mix_bus", ratio=2.0, attack_ms=30,
                              release_ms=250, gain_reduction_db=2.0))
    return recipe


def _match_stem(stems: list[str], role: str) -> str | None:
    """Map a role to an actual exported stem by name; no invented targets."""
    for path in stems or []:
        if role in path.lower():
            return path
    return None
