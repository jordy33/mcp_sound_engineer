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
import os
import re

RULES_VERSION = "2.2.0"
RECIPE_SCHEMA_VERSION = "2.2.0"

# --- Gates: when to refuse to master a mix -----------------------------------
GATE = {
    "max_corrective_db_per_point": 3.0,
    "max_cumulative_corrective_db": 6.0,
    "min_phase_correlation": 0.3,
    "noise_floor_moderate_db": -50.0,
    "noise_floor_minor_db": -60.0,
    "lossy_shelf_drop_db": 20.0,
    "max_limiter_gr_db": 4.0,
    # PRODUCT RULE: peaks this far over the ceiling are trimmed at the limiter input.
    # Beyond cap + trim the master is sent back instead of over-limiting it.
    "max_limiter_input_trim_db": 3.0,
    "max_gain_reduction_db": 3.0,
    "min_gain_reduction_db": 1.0,
    "max_total_gain_reduction_db": 6.0,   # conservative end of the 6-8 dB source range
    "min_mono_cancel_db": -6.0,
}

# Which GATE constants are actually enforced by code (tests assert completeness).
IMPLEMENTED_GATES = {
    "max_corrective_db_per_point", "max_cumulative_corrective_db",
    "min_phase_correlation", "max_limiter_gr_db", "max_limiter_input_trim_db",
    "max_gain_reduction_db", "min_gain_reduction_db", "noise_floor_moderate_db",
    "noise_floor_minor_db", "lossy_shelf_drop_db", "max_total_gain_reduction_db",
    "min_mono_cancel_db",
}
UNIMPLEMENTED_GATES: set[str] = set()

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
    # input_gain_db is the control (dB into the limiter); expected GR is a prediction.
    ("limiter",        {"ceiling_dbtp": -1.0, "max_gr_db": 4.0,
                        "max_input_trim_db": 3.0}),
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

# --- Reference curve (dB relative to the six measured bands) ------------------
# A generic fallback, NOT a genre target: it is used only when no measured
# reference was supplied. Normalized so the six linear powers sum to 1.0 (0 dB).
# `analysis.bands_db_rel` normalizes each band against the full-spectrum total,
# so its six-band sum is slightly below 0 when energy exists below 20 Hz or above
# 16 kHz. Renormalizing either curve shifts all of its bands by one constant,
# which cancels when the largest excess is chosen; it is documented in LIMITS.md.
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
    "polarity.invert": "flip polarity (phase, before any EQ)",
    "eq.high_pass_stem": "high-pass filter on one stem",
    "limiter":       "peak limiter",
    "fader":         "channel gain",
    "pan":           "stereo position",
    "dither":        "output bit-depth conversion",
    "send_back":     "stop: the mix needs work before mastering",
}
TARGETS = {"master", "mix_bus", "vocal", "drums", "bass", "bus", "stem"}
BAND_CENTERS = {"sub": 40, "bass": 150, "low_mid": 375, "mid": 1250, "high_mid": 4000, "high": 10000}

# --- Question schemas: the single source of truth for answer keys ------------
MASTER_QUESTIONS = {
    "low_end": {"type": "score",
                "instructions": "Low end vs reference_curve: how much low-mid/sub excess is there?",
                "criteria": ["well balanced", "slight excess", "noticeable", "heavy", "extreme"]},
    "high_mid": {"type": "score",
                 "instructions": "2-6 kHz vs reference_curve: how much presence excess is there?",
                 "criteria": ["none", "slight", "noticeable", "heavy", "extreme"]},
    "sibilance": {"type": "choice",
                  "instructions": "Which sibilance treatment fits, given spectral.sibilance_db_rel "
                                  "(5-9 kHz) and the reference's sibilance_db_rel when one was measured?",
                  "criteria": {"none": "no treatment needed",
                               "deess": "dynamic de-esser",
                               "cut": "narrow static cut around 7 kHz"}},
    "dynamics": {"type": "choice",
                 "instructions": "Dynamics work needed? LRA < 4 LU or crest < 6 dB means already compressed; "
                                 "upward expansion is not offered because it has no verified mapping.",
                 "criteria": {"none": "already dense, do not compress",
                              "glue": "gentle bus glue, 1-3 dB GR"}},
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
    "phase_before_eq": {"type": "choice",
                        "instructions": "Given per-stem phase_correlation, is polarity the first fix?",
                        "criteria": {"clean": "correlation is fine",
                                     "polarity": "a stem cancels; flip polarity before EQ"}},
    "hpf_policy": {"type": "choice",
                   "instructions": "Should everything except kick and bass be high-passed?",
                   "criteria": {"none": "no high-pass",
                                "all_but_kick_bass": "high-pass vocals, guitars, keys, room mics"}},
}

GAIN_STAGING_TARGET_LUFS = -18.0
GAIN_STAGING_MAX_MOVE_DB = 6.0
CONFIDENCE_MIN = 0.5

# Jev answers a `score` question on a saturating 2-10 scale (five criterion
# levels). The scale is abstract: every use must name the physical range it maps
# onto. Values outside the scale clamp to the ends of that range.
SCORE_MIN = 2.0
SCORE_MAX = 10.0

# PRODUCT RULE: masking at or below this score needs no corrective bell; above it
# the requested cut grows 1:1 with the score, capped by GATE.
MASKING_SCORE_BASE = 2.0
MASKING_SCORE_THRESHOLD = 2.5
# Only a shared mid/high band can be treated with a bus bell. A pair whose only
# overlap is sub/bass needs faders or a high-pass, not a 300 Hz bus cut.
MASKING_BANDS = ("low_mid", "mid", "high_mid", "high")
MASKING_MIN_BAND_DB = -15.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def map_score(score: float, low: float, high: float) -> float:
    """Map a 2-10 decision score linearly onto `low..high`.

    A saturated score cannot exceed the cap, and a score below the scale's floor
    cannot invent a move smaller than the range's floor. Raises on non-numeric
    input instead of defaulting to zero.
    """
    value = _number(score)
    if value is None:
        raise ValueError(f"score must be numeric, got {score!r}")
    fraction = clamp((value - SCORE_MIN) / (SCORE_MAX - SCORE_MIN), 0.0, 1.0)
    return low + fraction * (high - low)


def loudness_target(platform: str) -> dict:
    key = (platform or "").lower()
    if key not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}; choose one of {sorted(PLATFORMS)}")
    return PLATFORMS[key]


FIELD = {"noul": "noul", "score": "score", "choice": "choice"}


def require(answers: dict, questions: dict) -> None:
    """Fail loud when an answer is missing, or present without the field its type needs."""
    missing = [k for k in questions if k not in answers]
    if missing:
        raise ValueError(f"answers missing {missing}; schema defines {sorted(questions)}")
    malformed = [f"{k}.{FIELD[q['type']]}" for k, q in questions.items()
                 if FIELD[q["type"]] not in answers[k]]
    if malformed:
        raise ValueError(f"answers without the expected field: {malformed}")


def _number(value):
    """A finite float, or None. Booleans are not measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _count(value):
    """A non-negative count, or None."""
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _score(answers, name):
    return float(answers[name]["score"])


def _choice(answers, name, default=""):
    return str(answers.get(name, {}).get("choice", default))


def _action(action: str, target: str = "master", **params) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    if target not in TARGETS:
        raise ValueError(f"unknown target: {target}")
    return {"action": action, "target": target, "schema": RECIPE_SCHEMA_VERSION, **params}


def _slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"


def _finalize(recipe: list[dict]) -> list[dict]:
    """Give every action a stable, human-readable id.

    The id is derived from what the action does, not its array index, so a
    report after a bounce still points at the same action even if the recipe is
    regenerated with one more move. Duplicates get a numeric suffix.
    """
    seen: dict[str, int] = {}
    for item in recipe:
        if not isinstance(item, dict) or item.get("id"):
            continue
        parts = [item.get("action", "action"), item.get("target", "target")]
        if item.get("stem"):
            parts.append(os.path.splitext(os.path.basename(str(item["stem"])))[0])
        elif item.get("band"):
            parts.append(str(item["band"]))
        elif item.get("freq_hz") is not None:
            parts.append(f"{float(item['freq_hz']):g}hz")
        base = ":".join(_slug(p) for p in parts)
        seen[base] = seen.get(base, 0) + 1
        item["id"] = base if seen[base] == 1 else f"{base}:{seen[base]}"
    return recipe


# Public alias: the MCP server finalizes the measured send_back recipe too.
finalize_recipe = _finalize


def _expect_delta(metric: str, delta_db: float, tolerance_db: float,
                  stem: str | None = None) -> dict:
    expect = {"metric": metric, "delta_db": _round_db(delta_db),
              "tolerance_db": float(tolerance_db)}
    if stem:
        expect["stem"] = stem
    return expect


def _expect_max(metric: str, maximum: float, tolerance_db: float = 0.1) -> dict:
    return {"metric": metric, "max": _round_db(maximum),
            "tolerance_db": float(tolerance_db)}


def _stem_name(stem: dict) -> str:
    return str(stem.get("name") or os.path.splitext(
        os.path.basename(str(stem.get("path", ""))))[0])


def _band_overlap_plan(stem_facts: list[dict] | None) -> dict:
    """Pick the pair and frequency a masking bell should act on.

    The score says *how much* masking, but only the measured stems say *where*.
    Low-band overlap is deliberately excluded: a bus bell at 150 Hz would take
    weight out of the whole mix, while the actual fix is a fader or high-pass.
    """
    measured = False
    best = None
    facts = [s for s in stem_facts or [] if isinstance(s, dict)]
    for i in range(len(facts)):
        for j in range(i + 1, len(facts)):
            a, b = facts[i], facts[j]
            bands_a, bands_b = a.get("bands_db_rel"), b.get("bands_db_rel")
            if not isinstance(bands_a, dict) or not isinstance(bands_b, dict):
                continue
            values = {}
            for band in BAND_CENTERS:
                value_a, value_b = _number(bands_a.get(band)), _number(bands_b.get(band))
                if value_a is not None and value_b is not None:
                    values[band] = min(value_a, value_b)
            if not values:
                continue
            measured = True
            band = max(MASKING_BANDS, key=lambda k: values.get(k, -999.0))
            if values.get(band, -999.0) < MASKING_MIN_BAND_DB:
                continue
            if best is None or values[band] > best["overlap_db"]:
                best = {"status": "measured", "band": band,
                        "freq_hz": BAND_CENTERS[band],
                        "stems": [_stem_name(a), _stem_name(b)],
                        "overlap_db": round(values[band], 1)}
    if best is not None:
        return best
    return {"status": "low_band_only" if measured else "unmeasured",
            "band": None, "freq_hz": None, "stems": [], "overlap_db": None}


def _masking_plan(answers: dict, stem_facts: list[dict] | None) -> dict | None:
    score = _score(answers, "masking")
    if score < MASKING_SCORE_THRESHOLD:
        return None
    plan = _band_overlap_plan(stem_facts)
    plan["score"] = score
    plan["cut_db"] = _round_db(clamp(score - MASKING_SCORE_BASE, 0.0,
                                     GATE["max_corrective_db_per_point"]))
    return plan


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------
GATE_THRESHOLD = 0.8
MIN_CLIPPED_SAMPLES = 500
MIN_CLIPPED_PROPORTION = 0.0001
MAX_LRA_FINISHED = 4.0


def evaluate_gates(facts: dict) -> dict:
    """Refuse mastering, and say exactly why. Every condition here is measured.

    A gate whose input is absent is NOT treated as healthy: the missing field is
    reported as `unmeasured:<field>` and the result is a refusal. There is no
    "default to a safe-looking number" path. The one exception is
    `noise.floor_dbfs: null`, which is itself a measurement: it means the file
    has no silence in which to measure a floor.

    The decision model is deliberately NOT asked whether to refuse: a global yes/no
    duplicates the detailed scores and contradicts them when they disagree.
    """
    reasons: list[str] = []
    unmeasured: list[str] = []
    facts = facts if isinstance(facts, dict) else {}

    stereo = facts.get("stereo")
    stereo = stereo if isinstance(stereo, dict) else {}
    phase = _number(stereo.get("phase_correlation"))
    if phase is None:
        unmeasured.append("stereo.phase_correlation")
    elif phase < GATE["min_phase_correlation"]:
        reasons.append("phase_problem:measured")

    loud = facts.get("loudness")
    loud = loud if isinstance(loud, dict) else {}
    clipping = facts.get("clipping")
    clipping = clipping if isinstance(clipping, dict) else {}
    # `count` is stereo FRAMES, not channel samples: analysis.clipping counts a
    # frame once even when both channels clip, so duration * sample_rate is the
    # correct denominator regardless of channel count.
    clipped = _count(clipping.get("count"))
    duration = _number(facts.get("duration_seconds"))
    sample_rate = _number(facts.get("sample_rate"))
    if clipped is None:
        unmeasured.append("clipping.count")
    if duration is None or duration <= 0:
        unmeasured.append("duration_seconds")
    if sample_rate is None or sample_rate <= 0:
        unmeasured.append("sample_rate")
    if clipped is not None and duration and sample_rate:
        frames = duration * sample_rate
        if clipped >= MIN_CLIPPED_SAMPLES and (clipped / frames) >= MIN_CLIPPED_PROPORTION:
            reasons.append("clipped:measured")

    noise = facts.get("noise")
    if not isinstance(noise, dict) or "floor_dbfs" not in noise:
        unmeasured.append("noise.floor_dbfs")
    elif noise.get("floor_dbfs") is not None:
        floor = _number(noise.get("floor_dbfs"))
        if floor is None:
            unmeasured.append("noise.floor_dbfs")
        elif floor > GATE["noise_floor_moderate_db"]:
            reasons.append("noise_floor:measured")

    bandwidth = facts.get("bandwidth")
    if not isinstance(bandwidth, dict) or not (
            "lossy_suspected" in bandwidth or "drop_db" in bandwidth):
        unmeasured.append("bandwidth")
    else:
        suspected = bandwidth.get("lossy_suspected")
        drop = _number(bandwidth.get("drop_db"))
        if "lossy_suspected" in bandwidth and not isinstance(suspected, bool):
            unmeasured.append("bandwidth.lossy_suspected")
        if "drop_db" in bandwidth and drop is None:
            unmeasured.append("bandwidth.drop_db")
        if suspected is True or (drop is not None and drop > GATE["lossy_shelf_drop_db"]):
            reasons.append("lossy_source:measured")

    # PRODUCT RULE (not in the source): a finished master cannot be mastered again.
    lra = _number(loud.get("loudness_range_lu"))
    true_peak = _number(loud.get("true_peak_dbtp"))
    integrated = _number(loud.get("integrated_lufs"))
    for field, value in (("loudness_range_lu", lra),
                         ("true_peak_dbtp", true_peak),
                         ("integrated_lufs", integrated)):
        if value is None:
            unmeasured.append("loudness." + field)
    if (lra is not None and true_peak is not None and integrated is not None
            and lra < MAX_LRA_FINISHED and true_peak > -1.0 and integrated > -12.0):
        reasons.append("already_mastered:measured")

    reasons.extend("unmeasured:" + field for field in unmeasured)
    return {"send_back": bool(reasons), "reasons": reasons,
            "unmeasured": unmeasured, "complete": not unmeasured}


def loudness_warnings(facts: dict, target: dict) -> list[str]:
    out: list[str] = []
    loud = facts.get("loudness") or {}
    lufs = float(loud.get("integrated_lufs", -99.0))
    tp = float(loud.get("true_peak_dbtp", -99.0))
    ceiling = target["true_peak_ceiling"]
    if tp > ceiling + 0.05:
        out.append(f"true peak {tp} dBTP is above the {ceiling} dBTP ceiling for this destination")
    stereo = facts.get("stereo") or {}
    cancel = stereo.get("mono_cancel_db")
    if cancel is not None and float(cancel) < GATE["min_mono_cancel_db"]:
        out.append(f"mono fold-down loses {abs(cancel)} dB; check phase before the chain")
    floor = (facts.get("noise") or {}).get("floor_dbfs")
    if floor is not None and GATE["noise_floor_minor_db"] < float(floor) <= GATE["noise_floor_moderate_db"]:
        out.append(f"noise floor {floor} dBFS is minor but audible on quiet passages")
    ref = target.get("normalization_lufs")
    if ref is not None and lufs > ref + 6:
        out.append(f"{round(lufs - ref, 1)} dB hotter than the {ref} LUFS playback reference — "
                   f"the service will turn it down, so those extra dB buy nothing and cost dynamics")
    return out


def _round_db(value: float) -> float:
    """One decimal; never emit JSON's -0.0."""
    rounded = round(float(value), 1)
    return 0.0 if rounded == 0 else rounded


def plan_limiter(lufs: float, tp: float, ceiling: float,
                 desired_lufs: float | None = None,
                 cap: float = GATE["max_limiter_gr_db"]) -> dict:
    """Two constraints, not one: the requested loudness and the peak ceiling.

    `input_gain_db` is the control written to the limiter;
    `expected_gain_reduction_db` is a prediction from the measured loudness and true
    peak. There is no delivery loudness target: without `desired_lufs` the plan is
    peak-only, which is the recommended mode for streaming (playback is normalized
    anyway, so extra input gain buys no loudness and costs dynamics).
    """
    g_loud = 0.0 if desired_lufs is None else max(0.0, desired_lufs - lufs)
    g_max = ceiling + cap - tp
    gain = min(g_loud, g_max)
    gr = max(0.0, tp + gain - ceiling)
    # Algebraically identical to ceiling-(tp-lufs)+gr whenever gr > 0; without
    # limiting it reports the real projection instead of overestimating loudness.
    approx = _round_db(lufs + gain)
    return {
        "input_gain_db": _round_db(gain),
        "expected_gain_reduction_db": _round_db(gr),
        "loudness_reached": True if desired_lufs is None else approx >= desired_lufs - 0.2,
        "approx_lufs_after": approx,
        "plr_db": _round_db(tp - lufs),
    }


_NEUTRAL_LIMITER_PLAN = {
    "input_gain_db": 0.0,
    "expected_gain_reduction_db": 0.0,
    "loudness_reached": True,
    "approx_lufs_after": None,
    "plr_db": None,
}


def limiter_warnings(recipe: list) -> list[str]:
    """Surface a pre-limiter trim or a loudness request the limiter cannot meet."""
    out: list[str] = []
    limiter = next((a for a in recipe or []
                    if isinstance(a, dict) and a.get("action") == "limiter"), None)
    if limiter is None:
        return out
    gain = limiter.get("input_gain_db")
    if isinstance(gain, (int, float)) and not isinstance(gain, bool) and gain < 0:
        out.append(f"input trim {abs(gain)} dB before the limiter: the true peak exceeds "
                   f"the ceiling by more than the {GATE['max_limiter_gr_db']} dB "
                   f"gain-reduction budget")
    if limiter.get("desired_lufs") is not None and limiter.get("loudness_reached") is False:
        out.append(f"loudness request {limiter['desired_lufs']} LUFS not reached: approx "
                   f"{limiter.get('approx_lufs_after')} LUFS after the limiter "
                   f"(PLR {limiter.get('plr_db')} dB); fix dynamics upstream instead of "
                   f"pushing the limiter")
    return out


def master_warnings(answers: dict) -> list[str]:
    """Name decisions the recipe deliberately does not turn into an action."""
    out: list[str] = []
    if _choice(answers, "dynamics") == "expand":
        out.append("upward expansion was requested but is not emitted: it has no verified "
                   "mapping, and over-compressed material is an upstream problem. Dynamics "
                   "are unchanged; fix the mix or leave it alone.")
    return out


def _limiter_plan(facts: dict, target: dict, desired_lufs: float | None) -> dict:
    loud = facts.get("loudness") or {}
    lufs, tp = loud.get("integrated_lufs"), loud.get("true_peak_dbtp")
    if lufs is None or tp is None:
        # No measurement, no invented dB.
        return dict(_NEUTRAL_LIMITER_PLAN)
    return plan_limiter(float(lufs), float(tp), target["true_peak_ceiling"], desired_lufs)


def build_master_recipe(answers: dict, facts: dict, platform: str = "spotify",
                        reference_curve: dict | None = None,
                        desired_lufs: float | None = None) -> list[dict]:
    require(answers, MASTER_QUESTIONS)
    target = loudness_target(platform)

    reference = REFERENCE_CURVE if reference_curve is None else reference_curve
    if not isinstance(reference, dict):
        raise ValueError("reference_curve must be a dict of the six bands")
    missing = [band for band in BAND_CENTERS if band not in reference]
    if missing:
        # A partial measured curve must not be silently mixed with the default.
        raise ValueError(f"reference_curve is missing bands: {missing}")

    gates = evaluate_gates(facts)
    if gates["send_back"]:
        return _finalize([_action("send_back", reasons=gates["reasons"])])

    limiter_plan = _limiter_plan(facts, target, desired_lufs)
    ceiling = target["true_peak_ceiling"]
    tp = (facts.get("loudness") or {}).get("true_peak_dbtp")
    if tp is not None and (float(tp) - ceiling) > (GATE["max_limiter_gr_db"]
                                                    + GATE["max_limiter_input_trim_db"]):
        return _finalize([_action("send_back",
                                  reasons=[f"peak_beyond_limiter:{float(tp)}dBTP:{ceiling}dBTP"])])

    recipe: list[dict] = [_action("eq.high_pass", freq_hz=25, slope_db_oct=12, reason="sub-bass cleanup")]

    # Corrective moves: over the cap is a send-back, never a silent clamp.
    total = 0.0
    for band, group in (("low_end", ("sub", "bass", "low_mid")), ("high_mid", ("mid", "high_mid"))):
        level = _score(answers, band)
        if level < 2.5:
            continue
        measured = (facts.get("spectral") or {}).get("bands_db_rel") or {}
        excess = {b: float(measured.get(b, reference[b])) - reference[b] for b in group}
        worst = max(excess, key=excess.get)
        freq = BAND_CENTERS[worst]
        needed = (level - 2.0) * 2.0   # level 4 (extreme) demands 4 dB -> send back
        if needed > GATE["max_corrective_db_per_point"]:
            return _finalize([_action("send_back",
                                      reasons=[f"corrective_beyond_cap:{band}:{round(needed, 1)}dB"])])
        total += needed
        if total > GATE["max_cumulative_corrective_db"]:
            return _finalize([_action("send_back",
                                      reasons=[f"corrective_beyond_cap:cumulative:{round(total, 1)}dB"])])
        recipe.append(_action("eq.bell", freq_hz=freq, gain_db=-round(needed, 1), q=4.0,
                              role="corrective", measured_from=band, band=worst,
                              expect=_expect_delta(f"spectral.bands_db_rel.{worst}",
                                                   -round(needed, 1), 1.5)))

    # Dynamics before sibilance: the de-esser belongs to the dynamic stage, after compression.
    if _choice(answers, "dynamics") == "glue":
        # The score is abstract; name the physical range instead of treating it as dB.
        gr = map_score(_score(answers, "dynamics_amount"),
                       GATE["min_gain_reduction_db"], GATE["max_gain_reduction_db"])
        recipe.append(_action("compressor", target="master", ratio=2.0, attack_ms=20,
                              release_ms=200, gain_reduction_db=round(gr, 1)))
    # `expand` is deliberately not emitted: it has no verified mapping and an
    # already over-compressed master is an upstream problem, not an EQ-shaped one.

    total_gr = sum(a.get("gain_reduction_db", 0.0) for a in recipe if a["action"] == "compressor")
    total_gr += limiter_plan["expected_gain_reduction_db"]
    if total_gr > GATE["max_total_gain_reduction_db"]:
        return _finalize([_action("send_back",
                                  reasons=[f"gain_reduction_budget:{round(total_gr, 1)}dB"])])

    sib = _choice(answers, "sibilance")
    if sib == "deess":
        recipe.append(_action("de_esser", freq_hz=6500, ratio=2.0))
    elif sib == "cut":
        needed = 1.5
        total += needed
        if total > GATE["max_cumulative_corrective_db"]:
            return _finalize([_action("send_back",
                                      reasons=[f"corrective_beyond_cap:cumulative:{round(total, 1)}dB"])])
        recipe.append(_action("eq.bell", freq_hz=7000, gain_db=-needed, q=6.0, role="corrective",
                              expect=_expect_delta("spectral.sibilance_db_rel", -needed, 1.5)))

    limiter_params = {
        "ceiling_dbtp": ceiling,
        "input_gain_db": limiter_plan["input_gain_db"],
        "expected_gain_reduction_db": limiter_plan["expected_gain_reduction_db"],
        "loudness_reached": limiter_plan["loudness_reached"],
        "approx_lufs_after": limiter_plan["approx_lufs_after"],
        "plr_db": limiter_plan["plr_db"],
    }
    if desired_lufs is not None:
        limiter_params["desired_lufs"] = desired_lufs
    limiter_params["expect"] = _expect_max("loudness.true_peak_dbtp", ceiling)
    recipe.append(_action("limiter", **limiter_params))

    # Dither belongs to the destination, and only when bits are actually reduced.
    source_bits = facts.get("bit_depth")
    reducing = source_bits is None or target["bit_depth"] < int(source_bits)
    if target["dither"] != "none" and reducing:
        recipe.append(_action("dither", bit_depth=target["bit_depth"], type=target["dither"]))
    return _finalize(recipe)


def correlation_drop(before: dict, after: dict, limit: float = 0.15) -> dict:
    """The source's post-chain check: cumulative phase rotation must not eat the image.

    Missing correlation on either side is reported as unmeasured, never as a
    healthy 1.0 that makes the drop look like zero.
    """
    before_stereo = before.get("stereo") if isinstance(before, dict) else None
    after_stereo = after.get("stereo") if isinstance(after, dict) else None
    b = _number((before_stereo if isinstance(before_stereo, dict) else {})
                .get("phase_correlation"))
    a = _number((after_stereo if isinstance(after_stereo, dict) else {})
                .get("phase_correlation"))
    unmeasured = [side for side, value in (("before", b), ("after", a)) if value is None]
    if unmeasured:
        return {"before": b, "after": a, "drop": None, "exceeds": False,
                "unmeasured": unmeasured}
    drop = round(b - a, 3)
    return {"before": b, "after": a, "drop": drop, "exceeds": drop > limit,
            "unmeasured": []}


def _metric_value(facts, metric: str, stem_facts: dict | None, stem_key: str | None):
    """Resolve `spectral.bands_db_rel.mid` or `stem.integrated_lufs` to a number."""
    if not isinstance(facts, dict) or not isinstance(metric, str):
        return None
    if metric.startswith("stem."):
        if not isinstance(stem_facts, dict) or not isinstance(stem_key, str):
            return None
        key = os.path.splitext(os.path.basename(stem_key))[0]
        facts = stem_facts.get(key)
        if not isinstance(facts, dict):
            return None
        metric = metric[5:]
    value = facts
    for part in metric.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return _number(value)


def verify_expectations(recipe: list, before: dict, after: dict,
                        stems_before: dict | None = None,
                        stems_after: dict | None = None) -> list[dict]:
    """Check each action's measurable `expect` after a bounce.

    A missing metric is `unmeasured`, never `met`: the loop must not claim an
    effect it did not measure. The result is keyed by the action's `id`.
    """
    checks: list[dict] = []
    for index, action in enumerate(recipe or []):
        if not isinstance(action, dict) or not isinstance(action.get("expect"), dict):
            continue
        expect = action["expect"]
        metric = expect.get("metric")
        stem_key = expect.get("stem")
        before_value = _metric_value(before, metric, stems_before, stem_key)
        after_value = _metric_value(after, metric, stems_after, stem_key)
        check = {
            "id": action.get("id") or f"action[{index}]",
            "action": action.get("action"),
            "metric": metric,
            "expect": dict(expect),
            "before": before_value,
            "after": after_value,
            "status": "unmeasured",
        }
        if before_value is None or after_value is None:
            check["reason"] = "metric not measured on both sides"
        elif "delta_db" in expect:
            delta = round(after_value - before_value, 2)
            tolerance = _number(expect.get("tolerance_db")) or 0.0
            check["measured_delta_db"] = delta
            check["status"] = ("met" if abs(delta - float(expect["delta_db"]))
                               <= tolerance else "missed")
        elif "max" in expect:
            tolerance = _number(expect.get("tolerance_db")) or 0.0
            check["status"] = ("met" if after_value <= float(expect["max"]) + tolerance
                               else "missed")
        elif "min" in expect:
            tolerance = _number(expect.get("tolerance_db")) or 0.0
            check["status"] = ("met" if after_value >= float(expect["min"]) - tolerance
                               else "missed")
        else:
            check["reason"] = "expectation has no delta_db, max or min"
        checks.append(check)
    return checks


def _expect_violations(action: dict, index: int) -> list[dict]:
    expect = action.get("expect")
    if expect is None:
        return []
    if not isinstance(expect, dict):
        return [{"code": "expect_not_object", "index": index}]
    out: list[dict] = []
    metric = expect.get("metric")
    if not isinstance(metric, str) or not metric.strip():
        out.append({"code": "expect_metric_invalid", "index": index})
    comparators = [key for key in ("delta_db", "max", "min") if key in expect]
    if len(comparators) != 1:
        out.append({"code": "expect_comparator", "index": index})
    for key in comparators:
        if _number(expect.get(key)) is None:
            out.append({"code": "expect_value_invalid", "index": index, "field": key})
    tolerance = expect.get("tolerance_db", 0.0)
    if _number(tolerance) is None or float(tolerance) < 0:
        out.append({"code": "expect_tolerance_invalid", "index": index})
    if isinstance(metric, str) and metric.startswith("stem."):
        if not isinstance(expect.get("stem"), str) or not expect["stem"].strip():
            out.append({"code": "expect_stem_missing", "index": index})
    return out


def _limiter_violations(action: dict, index: int) -> list[dict]:
    """Check the control contract without ever raising on malformed values."""
    out: list[dict] = []
    numeric: dict[str, float] = {}
    for field, missing_code in (("ceiling_dbtp", "missing_limiter_ceiling"),
                                ("input_gain_db", "missing_limiter_input_gain"),
                                ("expected_gain_reduction_db", "missing_limiter_gr")):
        if field not in action:
            out.append({"code": missing_code, "index": index})
            continue
        value = action[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            out.append({"code": "invalid_limiter_field", "index": index, "field": field})
            continue
        numeric[field] = float(value)
    if numeric.get("ceiling_dbtp", -1.0) > -1.0:
        out.append({"code": "limiter_ceiling", "index": index,
                    "ceiling_dbtp": numeric["ceiling_dbtp"]})
    if numeric.get("input_gain_db", 0.0) < -GATE["max_limiter_input_trim_db"]:
        out.append({"code": "limiter_input_trim_cap", "index": index,
                    "input_gain_db": numeric["input_gain_db"]})
    if numeric.get("expected_gain_reduction_db", 0.0) > GATE["max_limiter_gr_db"]:
        out.append({"code": "limiter_cap", "index": index})
    return out


def validate_recipe(recipe: list) -> list[dict]:
    """Adapter-side contract check: run it before touching the session."""
    if not isinstance(recipe, list):
        return [{"code": "not_a_list"}]
    violations: list[dict] = []
    send_back = [a for a in recipe if isinstance(a, dict) and a.get("action") == "send_back"]
    if send_back and len(recipe) > 1:
        violations.append({"code": "send_back_not_terminal"})
    corrective = 0.0
    ids: dict[str, int] = {}
    for index, action in enumerate(recipe):
        if not isinstance(action, dict):
            violations.append({"code": "not_an_action", "index": index}); continue
        name = action.get("action")
        if name not in ACTIONS:
            violations.append({"code": "unknown_action", "index": index, "action": name}); continue
        if action.get("target") not in TARGETS:
            violations.append({"code": "unknown_target", "index": index})
        if action.get("schema") != RECIPE_SCHEMA_VERSION:
            violations.append({"code": "schema_mismatch", "index": index,
                               "schema": action.get("schema")})
        if "id" in action:
            action_id = action["id"]
            if not isinstance(action_id, str) or not action_id.strip():
                violations.append({"code": "action_id_invalid", "index": index})
            elif action_id in ids:
                violations.append({"code": "action_id_duplicate", "index": index,
                                   "id": action_id, "first_index": ids[action_id]})
            else:
                ids[action_id] = index
        violations.extend(_expect_violations(action, index))
        if name == "eq.bell" and action.get("role") == "corrective":
            gain = abs(float(action.get("gain_db", 0.0)))
            if gain > GATE["max_corrective_db_per_point"]:
                violations.append({"code": "corrective_cap", "index": index, "gain_db": gain})
            corrective += gain
        if name == "compressor" and float(action.get("gain_reduction_db", 0)) > GATE["max_gain_reduction_db"]:
            violations.append({"code": "gain_reduction_cap", "index": index})
        if name == "limiter":
            violations.extend(_limiter_violations(action, index))
        if name == "eq.high_pass" and float(action.get("freq_hz", 0)) > 30:
            violations.append({"code": "high_pass_range", "index": index})
        if name == "dither" and int(action.get("bit_depth", 24)) not in (16, 24):
            violations.append({"code": "dither_bit_depth", "index": index})
    if corrective > GATE["max_cumulative_corrective_db"]:
        violations.append({"code": "cumulative_cap", "total_db": round(corrective, 2)})
    return violations


def confidence_report(answers: dict, threshold: float = CONFIDENCE_MIN) -> dict:
    """Surface model answers that are close to a coin flip."""
    low = {name: round(float(answer.get("confidence", 1.0)), 2)
           for name, answer in answers.items()
           if float(answer.get("confidence", 1.0)) < threshold}
    return {"threshold": threshold, "low": low, "needs_human": bool(low)}


def mix_warnings(answers: dict, stems: list[str],
                 stem_facts: list[dict] | None = None) -> list[str]:
    """Say out loud when a decision cannot be applied to the exported files."""
    out: list[str] = []
    balance = _choice(answers, "balance")
    role = {"vocal_too_high": "vocal", "drums_too_loud": "drums",
            "bass_too_heavy": "bass"}.get(balance)
    if role and not _match_stem(stems, role):
        out.append(f"no exported stem matches {role!r}; the fader move was omitted "
                   f"(stems: {[s.split('/')[-1] for s in stems]})")

    plan = _masking_plan(answers, stem_facts)
    if plan:
        requested = plan["score"] - MASKING_SCORE_BASE
        if requested > GATE["max_corrective_db_per_point"]:
            where = (f"at {plan['freq_hz']} Hz" if plan["freq_hz"]
                     else "(frequency unmeasured)")
            out.append(f"masking score {plan['score']:g} asks for {round(requested, 1)} dB "
                       f"{where}, above the {GATE['max_corrective_db_per_point']} dB "
                       f"corrective cap; the bell is capped and the remaining imbalance is "
                       f"structural, not EQ-shaped")
        if plan["status"] == "unmeasured":
            out.append("masking was scored but no stem band data was measured; no bell was "
                       "emitted rather than guessing a frequency")
        elif plan["status"] == "low_band_only":
            out.append("the measured overlap is in sub/bass only; a bus EQ bell would not "
                       "fix it — rebalance the low end with faders or a high-pass")

    for stem in stem_facts or []:
        path = str(stem.get("path", ""))
        name = os.path.basename(path) or path
        lufs = _number(stem.get("integrated_lufs"))
        if lufs is None:
            out.append(f"{name}: loudness was not measured; no gain-staging move was planned")
            continue
        needed = GAIN_STAGING_TARGET_LUFS - lufs
        if abs(needed) > GAIN_STAGING_MAX_MOVE_DB:
            out.append(f"{name} needs {round(needed, 1):+g} dB to reach "
                       f"{GAIN_STAGING_TARGET_LUFS} LUFS; the fader is capped at "
                       f"±{GAIN_STAGING_MAX_MOVE_DB} dB — check the gain structure upstream")
    return out


def build_mix_recipe(answers: dict, stems: list[str], stem_facts: list[dict] | None = None) -> list[dict]:
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
            move = clamp(gain, -3.0, 3.0)
            recipe.append(_action("fader", target=target, gain_db=move, stem=stem,
                                  expect=_expect_delta("stem.integrated_lufs", move,
                                                       1.0, stem=stem)))
    plan = _masking_plan(answers, stem_facts)
    if plan and plan["status"] == "measured":
        # Over-cap requests are capped, and mix_warnings names the cap so the
        # hidden imbalance is not silently presented as an EQ-shaped problem.
        recipe.append(_action("eq.bell", target="bus", freq_hz=plan["freq_hz"],
                              gain_db=-plan["cut_db"], q=4.0, role="corrective",
                              band=plan["band"], stems=plan["stems"],
                              reason="measured_band_overlap",
                              expect=_expect_delta(
                                  f"spectral.bands_db_rel.{plan['band']}",
                                  -plan["cut_db"], 1.5)))
    # Gain staging is arithmetic, not taste: bring every stem toward -18 LUFS.
    for stem in stem_facts or []:
        lufs = _number(stem.get("integrated_lufs"))
        if lufs is None:
            continue   # unmeasured, not a healthy -18 LUFS
        move = clamp(GAIN_STAGING_TARGET_LUFS - lufs, -GAIN_STAGING_MAX_MOVE_DB,
                     GAIN_STAGING_MAX_MOVE_DB)
        if abs(move) >= 1.0:
            recipe.append(_action("fader", target="stem", gain_db=round(move, 1),
                                  stem=stem["path"], reason="gain_staging",
                                  expect=_expect_delta("stem.integrated_lufs",
                                                       round(move, 1), 1.0,
                                                       stem=stem["path"])))

    # Phase before EQ: a cancelling stem gets polarity, not a filter.
    if _choice(answers, "phase_before_eq") == "polarity":
        for stem in stem_facts or []:
            if float(stem.get("phase_correlation", 1.0)) < GATE["min_phase_correlation"]:
                recipe.append(_action("polarity.invert", target="stem", stem=stem["path"],
                                      reason="phase_correlation"))

    # High-pass policy: everything that is not a low instrument.
    if _choice(answers, "hpf_policy") == "all_but_kick_bass":
        for stem in stem_facts or []:
            if not stem.get("low_instrument", True):
                recipe.append(_action("eq.high_pass_stem", target="stem", stem=stem["path"],
                                      freq_hz=80, slope_db_oct=12))

    for stem in stem_facts or []:
        if float(stem.get("low_correlation", 1.0)) < GATE["min_phase_correlation"]:
            recipe.append(_action("stereo.width", target="bus", mono_below_hz=120,
                                  stem=stem["path"], reason="mono bass"))
    if _choice(answers, "bus_comp") == "glue":
        recipe.append(_action("compressor", target="mix_bus", ratio=2.0, attack_ms=30,
                              release_ms=250, gain_reduction_db=2.0))
    return _finalize(recipe)


STEM_SYNONYMS = {
    "vocal": ("vocal", "vox", "voice", "voz"),
    "drums": ("drum", "drums", "kick", "snare", "hat", "hats", "oh", "overhead",
              "perc", "loop", "beat", "kit"),
    "bass": ("bass", "808", "subbass", "sub_bass", "bassline"),
}

# Tokens that only ever belong to drums: a file named kick_sub.wav is a kick, not a bass.
DRUM_EXCLUSIVE = {"kick", "snare", "hat", "hats", "oh", "overhead", "perc", "kit"}


def _match_stem(stems: list[str], role: str) -> str | None:
    """Match a role against the basename, exact tokens first.

    - the path is never searched: '/Bounces/Vocals_Session/02_drums.wav' is drums
    - drum-exclusive tokens never match bass or vocal: 'kick_sub' is a kick
    - an exact token beats a substring, so 'bass_gtr' is not the bass if 'bass' exists
    """
    hints = STEM_SYNONYMS.get(role, (role,))
    scored: list[tuple[int, str]] = []
    for path in stems or []:
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        tokens = set(re.split(r"[^a-z0-9]+", stem))
        if role != "drums" and tokens & DRUM_EXCLUSIVE:
            continue
        best = 0
        for hint in hints:
            if hint in tokens:
                best = max(best, 3)
            elif hint in stem:
                best = max(best, 1)
        if best:
            scored.append((best, path))
    if not scored:
        return None
    scored.sort(key=lambda item: -item[0])
    return scored[0][1]
