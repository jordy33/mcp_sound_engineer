"""Professional mix/master rules. Every number here is a hard cap, not a suggestion.

Sources: mastering-engineer methodology (fadelabs/phantom) + 2026 platform loudness targets.
"""
from __future__ import annotations

RULES_VERSION = "1.0.0"

# --- Gates: when to refuse to master a mix -----------------------------------
GATE = {
    "max_corrective_db_per_point": 3.0,   # >3 dB at one frequency = mix problem
    "max_cumulative_corrective_db": 6.0,  # >6 dB total = systemic problem
    "min_phase_correlation": 0.3,         # below this, elements vanish in mono
    "noise_floor_moderate_db": -50.0,
    "noise_floor_minor_db": -60.0,
    "lossy_shelf_drop_db": 20.0,          # cliff above the HF shelf = lossy source
    "max_limiter_gr_db": 4.0,
    "max_gain_reduction_db": 3.0,         # broadband compression in mastering
}

# --- Chain: the ten stages, in order ----------------------------------------
CHAIN = [
    ("high_pass",   {"freq_hz": (20, 30), "slope_db_oct": (6, 12)}),
    ("corrective_eq", {"q": (4, 8), "max_gain_db": 3.0}),
    ("denoise",     {"order": ("tonal", "declick", "broadband")}),
    ("compression", {"ratio": (1.5, 3.0), "attack_ms": (10, 30), "gr_db": 3.0}),
    ("dynamic_eq",  {"crossovers_hz": (80, 120, 2000, 3000, 8000, 10000)}),
    ("tonal_eq",    {"q": (0.5, 1.5)}),
    ("stereo",      {"mono_below_hz": 120, "max_width_db": 2.0}),
    ("limiter",     {"ceiling_dbtp": -1.0, "max_gr_db": 4.0}),
    ("loudness",    {"integrated_lufs": "platform"}),
    ("dither",      {"always_last": True}),
]

# --- Loudness targets per platform ------------------------------------------
PLATFORMS = {
    "spotify":     {"lufs": -14.0, "true_peak": -1.0},
    "apple_music": {"lufs": -16.0, "true_peak": -1.0},
    "youtube":     {"lufs": -14.0, "true_peak": -1.0},
    "amazon":      {"lufs": -14.0, "true_peak": -2.0},
    "tidal":       {"lufs": -14.0, "true_peak": -1.0},
    "cd":          {"lufs": -9.0,  "true_peak": -0.1},
    "podcast":     {"lufs": -16.0, "true_peak": -1.0},
}

# --- Typical reference curve (dB relative to total), for comparison ---------
REFERENCE_CURVE = {
    "sub": -18.0, "bass": -11.0, "low_mid": -12.0,
    "mid": -8.0, "high_mid": -13.0, "high": -17.0,
}

# --- Logic Pro surface ------------------------------------------------------
LOGIC_PLUGINS = {
    "high_pass":    "Channel EQ",
    "corrective_eq": "Channel EQ",
    "tonal_eq":     "Channel EQ",
    "compression":  "Compressor",
    "dynamic_eq":   "Compressor (multiband)",
    "stereo":       "Stereo Spread",
    "limiter":      "Limiter",
    "dither":       "Bounce > Dither",
    "meter":        "Loudness Meter / Multimeter",
}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def loudness_target(platform: str) -> dict:
    return PLATFORMS.get(platform.lower(), PLATFORMS["spotify"])


def build_master_recipe(answers: dict, facts: dict, platform: str = "spotify") -> list[dict]:
    """Turn typed answers into Logic actions, clipped by the craft's hard caps."""
    recipe: list[dict] = []
    target = loudness_target(platform)

    def noul(name: str, default: float = 0.0) -> float:
        return float(answers.get(name, {}).get("noul", default))

    def score(name: str, default: float = 0.0) -> float:
        return float(answers.get(name, {}).get("score", default))

    def choice(name: str, default: str = "") -> str:
        return str(answers.get(name, {}).get("choice", default))

    if noul("send_back") >= 0.7:
        return [{"action": "send_back",
                 "reason": "corrective EQ or phase problems exceed mastering scope",
                 "confidence": round(noul("send_back"), 2)}]

    # 1. sub-bass cleanup
    recipe.append({"plugin": LOGIC_PLUGINS["high_pass"], "type": "high_pass",
                   "freq_hz": 25, "slope_db_oct": 12})

    # 2. corrective moves, capped at 3 dB each and 6 dB total
    total = 0.0
    for band, spec in (("low_mid", {"freq_hz": 300, "q": 5.0}),
                       ("high_mid", {"freq_hz": 3000, "q": 2.0})):
        level = score(band)                      # 0..4 scale from Jev
        if level >= 2.5:
            gain = -clamp((level - 2.0) * 1.5, 0.0, GATE["max_corrective_db_per_point"])
            gain = max(gain, -(GATE["max_cumulative_corrective_db"] - total))
            total += abs(gain)
            recipe.append({"plugin": LOGIC_PLUGINS["corrective_eq"], "type": "bell",
                           "freq_hz": spec["freq_hz"], "gain_db": round(gain, 1), "q": spec["q"]})

    # 3. sibilance
    sib = choice("sibilance")
    if sib == "deess":
        recipe.append({"plugin": "De-Esser", "freq_hz": 6500, "ratio": 2.0})
    elif sib == "cut":
        recipe.append({"plugin": LOGIC_PLUGINS["corrective_eq"], "type": "bell",
                       "freq_hz": 7000, "gain_db": -1.5, "q": 6.0})

    # 4. dynamics
    dyn = choice("dynamics")
    if dyn == "glue":
        recipe.append({"plugin": LOGIC_PLUGINS["compression"], "ratio": 2.0,
                       "attack_ms": 20, "release_ms": 200,
                       "gain_reduction_db": clamp(score("dynamics_amount") / 2, 0.5, 3.0)})
    elif dyn == "expand":
        recipe.append({"plugin": LOGIC_PLUGINS["compression"], "mode": "upward_expand",
                       "note": "restore range on over-compressed material"})

    # 5. limiter + loudness
    gr = clamp(score("limiter"), 0.0, GATE["max_limiter_gr_db"])
    recipe.append({"plugin": LOGIC_PLUGINS["limiter"], "ceiling_dbtp": target["true_peak"],
                   "expected_gain_reduction_db": round(gr, 1), "target_lufs": target["lufs"]})

    # 6. dither last
    recipe.append({"plugin": LOGIC_PLUGINS["dither"],
                   "bit_depth": 16 if choice("dither") == "tpdf_16" else 24})

    return recipe


def build_mix_recipe(answers: dict, stems: list[str]) -> list[dict]:
    """Mix decisions: fader moves capped at +/-3 dB, EQ cuts capped at 3 dB."""
    recipe: list[dict] = []
    balance = answers.get("balance", {}).get("choice")
    if balance and balance != "balanced":
        fader = {"vocal_forward": ("vocal", +2.0), "drums_forward": ("drums", +2.0),
                 "bass_heavy": ("bass", -2.0)}.get(balance)
        if fader:
            recipe.append({"target": fader[0], "action": "set_fader",
                           "gain_db": clamp(fader[1], -3.0, 3.0)})

    masking = float(answers.get("masking", {}).get("score", 0.0))
    if masking >= 2.5:
        recipe.append({"target": "bus", "action": "bell_cut",
                       "freq_hz": 300, "gain_db": -clamp((masking - 2.0) * 1.0, 0.0, 3.0), "q": 4.0})

    bus = answers.get("bus_comp", {}).get("choice")
    if bus == "glue":
        recipe.append({"target": "mix_bus", "action": "compressor",
                       "ratio": 2.0, "attack_ms": 30, "release_ms": 250, "gr_db": 2.0})
    return recipe
