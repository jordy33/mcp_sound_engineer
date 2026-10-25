"""Known-answer tests for the measurement layer.

Every test generates a signal whose correct value is known analytically, so a wrong
formula fails here instead of silently reaching a recipe.
"""
import os
import sys
import tempfile
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import analysis  # noqa: E402

SR = 44100


def _wav(samples: np.ndarray, sr: int = SR) -> str:
    """Write a float array (frames, channels) in [-1,1] to a temp WAV; returns the path."""
    path = os.path.join(tempfile.mkdtemp(), "t.wav")
    data = np.atleast_2d(samples.T).T if samples.ndim > 1 else samples.reshape(-1, 1)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(pcm.shape[1]); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def _sine(freq, amp, seconds=3.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float64)


def test_sample_peak_matches_amplitude():
    facts = analysis.analyze_master(_wav(_sine(1000, 0.5)))
    assert abs(facts["loudness"]["sample_peak_dbfs"] - (-6.02)) < 0.05, facts["loudness"]


def test_crest_factor_of_a_sine_is_3db():
    facts = analysis.analyze_master(_wav(_sine(1000, 0.5)))
    assert abs(facts["loudness"]["crest_db"] - 3.01) < 0.15, facts["loudness"]["crest_db"]


def test_true_peak_never_below_sample_peak():
    x = _sine(1000, 0.5) + 0.2 * _sine(11000, 0.4)   # inter-sample risk
    facts = analysis.analyze_master(_wav(x))["loudness"]
    assert facts["true_peak_dbtp"] >= facts["sample_peak_dbfs"] - 0.01, facts


def test_lufs_of_997hz_at_minus_20_dbfs():
    """BS.1770 is calibrated so a 997 Hz sine reads its own RMS level."""
    amp = 0.1 * np.sqrt(2)          # RMS = -20 dBFS
    lufs = analysis.integrated_lufs(_sine(997, amp), SR)
    assert abs(lufs - (-20.0)) < 1.0, lufs


def test_band_energy_is_localized():
    bass = analysis.bands_db_rel(_sine(100, 0.5), SR)
    high = analysis.bands_db_rel(_sine(8000, 0.5), SR)
    assert bass["bass"] > bass["high"], bass
    assert high["high"] > high["bass"], high


def test_phase_correlation_extremes():
    mono = _sine(440, 0.5)
    same = np.stack([mono, mono], axis=1)
    inverted = np.stack([mono, -mono], axis=1)
    assert analysis.analyze_master(_wav(same))["stereo"]["phase_correlation"] > 0.99
    assert analysis.analyze_master(_wav(inverted))["stereo"]["phase_correlation"] < -0.99


def test_clipping_counts_only_when_saturated():
    clean = analysis.analyze_master(_wav(_sine(440, 0.5)))["clipping"]["count"]
    flat_top = np.clip(_sine(440, 1.5), -1.0, 1.0)
    clipped = analysis.analyze_master(_wav(flat_top))["clipping"]["count"]
    assert clean == 0, clean
    assert clipped > 100, clipped


def test_silence_returns_finite_numbers():
    facts = analysis.analyze_master(_wav(np.zeros(SR)))
    assert np.isfinite(facts["loudness"]["integrated_lufs"])
    assert np.isfinite(facts["loudness"]["true_peak_dbtp"])
    assert facts["loudness"]["integrated_lufs"] < -60


def test_lufs_gating_ignores_silence():
    """BS.1770 gating: trailing silence must not drag integrated loudness down."""
    t = np.arange(int(3 * SR)) / SR
    loud = 0.3 * np.sin(2 * np.pi * 1000 * t)
    padded = np.concatenate([loud, np.zeros(7 * SR)])
    bare = analysis.integrated_lufs(loud, SR)
    with_silence = analysis.integrated_lufs(padded, SR)
    assert abs(bare - with_silence) < 1.0, (bare, with_silence)


def test_clipping_counts_frames_not_channels():
    """One clipped channel for N frames counts N, not 2N: the denominator is frames."""
    n = SR * 2
    x = np.zeros((n, 2))
    x[:, 1] = 0.2
    x[:100, 0] = 1.0                     # 100 clipped frames, left channel only
    counted = analysis.clipping(x)["count"]
    assert counted == 100, counted


def test_noise_floor_requires_silence():
    """Continuous music has no measurable floor; a gap does."""
    rng = np.random.default_rng(0)
    music = 0.2 * np.sin(2 * np.pi * 220 * np.arange(SR * 5) / SR)
    assert analysis.noise_floor_dbfs(music, SR) is None
    tone = 0.2 * np.sin(2 * np.pi * 220 * np.arange(SR * 3) / SR)
    hiss = 0.01 * rng.normal(size=SR * 2)             # RMS ~ -40 dBFS in the gap
    assert analysis.noise_floor_dbfs(np.concatenate([tone, hiss]), SR) > -45.0


def test_lossy_cutoff_detection():
    rng = np.random.default_rng(3)
    full = 0.1 * rng.normal(size=SR * 10)
    spec = np.fft.rfft(full)
    freqs = np.fft.rfftfreq(len(full), 1 / SR)
    spec[freqs > 16000] = 0                          # hard cliff, like a 128 kbps encode
    cut = np.fft.irfft(spec, n=len(full))
    assert analysis.spectral_cutoff(full, SR)["lossy_suspected"] is False
    assert analysis.spectral_cutoff(cut, SR)["lossy_suspected"] is True


def test_low_band_correlation_ignores_highs():
    t = np.arange(SR * 3) / SR
    bass = 0.4 * np.sin(2 * np.pi * 60 * t)
    high = 0.4 * np.sin(2 * np.pi * 8000 * t)   # igual amplitud: el full-band da ~0
    stereo = np.stack([bass + high, bass - high], axis=1)   # mono lows, inverted highs
    assert analysis.low_band_correlation(stereo, SR) > 0.99
    assert analysis.analyze_master(_wav(stereo))["stereo"]["phase_correlation"] < 0.5


def test_json_safe_output():
    import json
    facts = analysis.analyze_master(_wav(_sine(440, 0.3)))
    json.dumps(facts)          # numpy scalars would raise here


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
