"""Known-answer tests for the measurement layer.

Every test generates a signal whose correct value is known analytically, so a wrong
formula fails here instead of silently reaching a recipe.
"""
import os
import struct
import sys
import tempfile
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import analysis  # noqa: E402

SR = 44100


def _wav(samples: np.ndarray, sr: int = SR, name: str = "t.wav") -> str:
    """Write a float array (frames, channels) in [-1,1] to a temp WAV; returns the path."""
    path = os.path.join(tempfile.mkdtemp(), name)
    data = np.atleast_2d(samples.T).T if samples.ndim > 1 else samples.reshape(-1, 1)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(pcm.shape[1]); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def _wav24(samples: np.ndarray, sr: int = SR) -> str:
    """Write 24-bit PCM, which is what a Logic bounce defaults to."""
    path = os.path.join(tempfile.mkdtemp(), "t24.wav")
    data = np.atleast_2d(samples.T).T if samples.ndim > 1 else samples.reshape(-1, 1)
    values = (np.clip(data, -1, 1) * 8388607).astype(np.int32)
    raw = np.stack([values & 0xFF, (values >> 8) & 0xFF, (values >> 16) & 0xFF],
                   axis=-1).astype(np.uint8).tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(data.shape[1]); w.setsampwidth(3); w.setframerate(sr)
        w.writeframes(raw)
    return path


def _wav_float(samples: np.ndarray, sr: int = SR, bits: int = 32,
               extensible: bool = False) -> str:
    """Write WAVE_FORMAT_IEEE_FLOAT, which `wave` cannot round-trip."""
    path = os.path.join(tempfile.mkdtemp(), "tf.wav")
    data = np.atleast_2d(samples.T).T if samples.ndim > 1 else samples.reshape(-1, 1)
    dtype = {32: "<f4", 64: "<f8"}[bits]
    raw = data.astype(dtype).tobytes()
    channels = data.shape[1]
    block = channels * bits // 8
    if extensible:
        guid = struct.pack("<IHH", 3, 0, 0x0010) + b"\x80\x00\x00\xAA\x00\x38\x9B\x71"
        fmt = struct.pack("<HHIIHH", 0xFFFE, channels, sr, sr * block, block, bits)
        fmt += struct.pack("<HHI", 22, bits, 0) + guid       # cbSize, valid bits, mask
    else:
        fmt = struct.pack("<HHIIHH", 3, channels, sr, sr * block, block, bits)
    with open(path, "wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 20 + len(fmt) + len(raw)))
        handle.write(b"WAVE")
        handle.write(b"fmt ")
        handle.write(struct.pack("<I", len(fmt)))
        handle.write(fmt)
        handle.write(b"data")
        handle.write(struct.pack("<I", len(raw)))
        handle.write(raw)
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


def test_lufs_sums_channel_power():
    """Uncorrelated L/R must read ~6 dB above the same signal averaged to mono."""
    rng = np.random.default_rng(4)
    left = 0.05 * rng.normal(size=SR * 4)
    right = 0.05 * rng.normal(size=SR * 4)
    stereo = analysis.integrated_lufs(np.stack([left, right], axis=1), SR)
    mono = analysis.integrated_lufs(((left + right) / 2).reshape(-1, 1), SR)
    assert abs((stereo - mono) - 6.0) < 1.0, (stereo, mono)


def test_lufs_survives_out_of_phase_channels():
    rng = np.random.default_rng(5)
    left = 0.05 * rng.normal(size=SR * 4)
    lufs = analysis.integrated_lufs(np.stack([left, -left], axis=1), SR)
    assert lufs > -30, lufs          # the old mono average read silence here


def test_true_peak_is_accurate_on_a_pure_tone():
    tone = _sine(997, 0.5)
    tp = analysis.true_peak_dbtp(tone, SR)
    assert abs(tp - (-6.02)) < 0.1, tp


def test_true_peak_sees_a_hard_panned_channel():
    t = np.arange(SR * 2) / SR
    stereo = np.zeros((len(t), 2))
    stereo[:, 0] = 0.9 * np.sin(2 * np.pi * 440 * t)     # -0.92 dBFS in L only
    assert analysis.true_peak_dbtp(stereo, SR) > -1.5, analysis.true_peak_dbtp(stereo, SR)


def test_24_bit_wav_is_read_at_the_right_level():
    tone = _sine(1000, 0.5)
    path = _wav24(tone)
    facts = analysis.analyze_master(path)
    assert facts["bit_depth"] == 24
    assert abs(facts["loudness"]["sample_peak_dbfs"] - (-6.02)) < 0.1, facts["loudness"]


def test_mono_cancel_is_reported():
    t = np.arange(SR * 2) / SR
    tone = 0.3 * np.sin(2 * np.pi * 300 * t)
    stereo = np.stack([tone, -tone], axis=1)
    assert analysis.mono_cancel_db(stereo) < -40


def test_band_energy_is_localized():
    bass = analysis.bands_db_rel(_sine(100, 0.5), SR)
    high = analysis.bands_db_rel(_sine(8000, 0.5), SR)
    assert bass["bass"] > bass["high"], bass
    assert high["high"] > high["bass"], high


def test_sibilance_band_is_measured():
    sibilant = analysis.sibilance_db_rel(_sine(7000, 0.5), SR)
    low = analysis.sibilance_db_rel(_sine(440, 0.5), SR)
    assert sibilant > -3.0, sibilant
    assert low < -40.0, low


def test_reference_includes_sibilance():
    ref = analysis.analyze_reference(_wav(_sine(7000, 0.3)))
    assert np.isfinite(ref["sibilance_db_rel"]), ref


def test_stem_includes_sibilance():
    facts = analysis.analyze_stems([
        _wav(_sine(7000, 0.2), name="01_vocals.wav"),
        _wav(_sine(100, 0.2), name="02_bass.wav"),
    ])
    values = {s["name"]: s["sibilance_db_rel"] for s in facts["stems"]}
    assert all(np.isfinite(v) for v in values.values()), values
    assert values["01_vocals"] > values["02_bass"], values


def test_band_inventory_stays_six():
    """Sibilance is measured, but it is not a seventh band of the reference curve."""
    facts = analysis.analyze_master(_wav(_sine(440, 0.3)))
    assert len(analysis.BANDS) == 6
    assert set(facts["spectral"]["bands_db_rel"]) == set(analysis.BANDS)
    assert "sibilance" not in facts["spectral"]["bands_db_rel"]


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


def test_stem_gate_vocal_buried():
    t = np.arange(SR * 4) / SR
    facts = analysis.analyze_stems([
        _wav(0.02 * np.sin(2 * np.pi * 300 * t), name="01_vocals.wav"),
        _wav(0.5 * np.sin(2 * np.pi * 100 * t), name="02_drums.wav"),
        _wav(0.4 * np.sin(2 * np.pi * 60 * t), name="03_bass.wav"),
    ])
    assert [g["gate"] for g in facts["gates"]] == ["vocal_buried"], facts["gates"]


def test_stem_gate_clean_mix_has_no_gates():
    t = np.arange(SR * 4) / SR
    facts = analysis.analyze_stems([
        _wav(0.3 * np.sin(2 * np.pi * 300 * t), name="01_vocals.wav"),
        _wav(0.3 * np.sin(2 * np.pi * 100 * t), name="02_drums.wav"),
        _wav(0.3 * np.sin(2 * np.pi * 60 * t), name="03_bass.wav"),
    ])
    assert facts["gates"] == [], facts["gates"]


def test_stem_gate_mono_bass():
    t = np.arange(SR * 4) / SR
    bass, high = 0.4 * np.sin(2 * np.pi * 60 * t), 0.2 * np.sin(2 * np.pi * 5000 * t)
    facts = analysis.analyze_stems([_wav(np.stack([bass + high, -bass + high], axis=1), name="02_bass.wav")])
    assert facts["gates"][0]["gate"] == "mono_bass", facts["gates"]


def test_sections_flag_a_hot_window():
    t = np.arange(SR * 24) / SR
    quiet = 0.05 * np.sin(2 * np.pi * 220 * t[: SR * 6])
    loud = 0.9 * np.sin(2 * np.pi * 220 * t[: SR * 6])
    signal = np.concatenate([quiet, quiet, quiet, loud])
    facts = analysis.analyze_sections(_wav(signal), window_s=6.0)
    assert facts["reliable"] is True
    assert len(facts["flagged_windows"]) == 1, facts["flagged_windows"]
    assert facts["flagged_windows"] == [18.0], facts["flagged_windows"]


def test_sections_refuses_to_flag_two_windows():
    t = np.arange(SR * 12) / SR
    quiet = 0.05 * np.sin(2 * np.pi * 220 * t[: SR * 6])
    loud = 0.9 * np.sin(2 * np.pi * 220 * t[: SR * 6])
    facts = analysis.analyze_sections(_wav(np.concatenate([quiet, loud])), window_s=6.0)
    assert facts["reliable"] is False
    assert facts["flagged_windows"] == []


def test_reference_curve_is_measurable():
    t = np.arange(SR * 5) / SR
    ref = analysis.analyze_reference(_wav(0.3 * np.sin(2 * np.pi * 80 * t)))
    assert set(ref["reference_curve"]) == {"sub", "bass", "low_mid", "mid", "high_mid", "high"}
    assert ref["reference_curve"]["bass"] > ref["reference_curve"]["high"]


def test_json_safe_output():
    import json
    facts = analysis.analyze_master(_wav(_sine(440, 0.3)))
    json.dumps(facts)          # numpy scalars would raise here


# ---------------------------------------------------------------------------
# sample-rate, stereo, edge and R128 regressions
# ---------------------------------------------------------------------------
def _fallback_lufs(x, sr):
    """Run integrated_lufs without pyloudnorm, even when it is installed."""
    import sys
    saved = sys.modules.get("pyloudnorm")
    sys.modules["pyloudnorm"] = None
    try:
        return analysis.integrated_lufs(x, sr)
    finally:
        if saved is None:
            del sys.modules["pyloudnorm"]
        else:
            sys.modules["pyloudnorm"] = saved


def test_k_weighting_coefficients_are_derived_for_48k():
    """The ITU parameterisation must reproduce the published 48 kHz constants."""
    b1, a1, b2, a2 = analysis._k_coefficients(48000)
    np.testing.assert_allclose(
        b1, [1.53512485958697, -2.69169618940638, 1.19839281085285], atol=1e-6)
    np.testing.assert_allclose(
        a1, [1.0, -1.69065929318241, 0.73248077421585], atol=1e-6)
    np.testing.assert_allclose(
        a2, [1.0, -1.99004745483398, 0.99007225036621], atol=1e-6)
    assert not np.allclose(analysis._k_coefficients(96000)[0], b1)


def test_fallback_lufs_is_correct_at_any_sample_rate():
    """Fixed 48 kHz coefficients misread 44.1/96 kHz files by up to ~1.7 LU."""
    for sr in (44100, 48000, 96000):
        t = np.arange(sr * 4) / sr
        x = (0.1 * np.sqrt(2) * np.sin(2 * np.pi * 997 * t)).reshape(-1, 1)
        assert abs(_fallback_lufs(x, sr) - (-20.0)) < 0.5, (sr, _fallback_lufs(x, sr))


def test_fallback_lufs_matches_pyloudnorm_when_installed():
    try:
        import pyloudnorm as pyln
    except ImportError:
        return                                # optional dependency: nothing to compare
    for sr in (44100, 48000, 96000):
        t = np.arange(sr * 8) / sr
        mono = (0.15 * np.sin(2 * np.pi * 110 * t) + 0.08 * np.sin(2 * np.pi * 997 * t)
                + 0.05 * np.sin(2 * np.pi * 3000 * t)
                + 0.02 * np.random.default_rng(0).normal(size=len(t)))
        x = np.stack([mono, mono], axis=1)
        exact = pyln.Meter(sr).integrated_loudness(x)
        assert abs(_fallback_lufs(x, sr) - exact) < 0.3, (sr, exact)


def test_reference_loudness_is_not_a_mono_fold_down():
    """Correlated stereo reads ~3 LU above the same signal averaged to mono."""
    t = np.arange(SR * 5) / SR
    mono = 0.1 * np.sqrt(2) * np.sin(2 * np.pi * 997 * t)
    ref = analysis.analyze_reference(_wav(np.stack([mono, mono], axis=1)))
    folded = analysis.integrated_lufs(mono.reshape(-1, 1), SR)
    assert abs((ref["integrated_lufs"] - folded) - 3.0) < 0.2, (ref["integrated_lufs"], folded)


def test_reference_true_peak_survives_out_of_phase_stereo():
    """A mono fold-down cancels this file; true peak must still see it."""
    t = np.arange(SR * 2) / SR
    left = 0.6 * np.sin(2 * np.pi * 997 * t)
    ref = analysis.analyze_reference(_wav(np.stack([left, -left], axis=1)))
    assert ref["true_peak_dbtp"] > -5.0, ref["true_peak_dbtp"]


def test_section_loudness_is_measured_in_stereo():
    t = np.arange(SR * 18) / SR
    mono = 0.1 * np.sqrt(2) * np.sin(2 * np.pi * 997 * t)
    facts = analysis.analyze_sections(_wav(np.stack([mono, mono], axis=1)), window_s=6.0)
    folded = analysis.integrated_lufs(mono[: SR * 6].reshape(-1, 1), SR)
    assert abs((facts["windows"][0]["integrated_lufs"] - folded) - 3.0) < 0.2


def test_true_peak_covers_the_first_and_last_samples():
    """The old block estimate discarded a quarter block at each edge."""
    n = SR * 2
    for pos in (0, n // 2, -1, -100):
        x = np.zeros(n)
        x[pos] = 0.9
        tp = analysis.true_peak_dbtp(x, SR)
        assert abs(tp - (-0.92)) < 0.2, (pos, tp)


def test_loudness_range_gates_silence():
    """Unweighted RMS windows counted silence and inflated LRA past 100 LU."""
    t = np.arange(SR * 10) / SR
    tone = 0.2 * np.sin(2 * np.pi * 997 * t)
    assert analysis.loudness_range(tone, SR) < 1.0
    with_silence = np.concatenate([tone, np.zeros(SR * 10)])
    assert analysis.loudness_range(with_silence, SR) < 6.0


def test_loudness_range_measures_dynamics():
    t = np.arange(SR * 6) / SR
    loud = 0.2 * np.sin(2 * np.pi * 997 * t)
    quiet = 0.02 * np.sin(2 * np.pi * 997 * t)
    lra = analysis.loudness_range(np.concatenate([loud, quiet, loud, quiet]), SR)
    assert 15.0 < lra < 25.0, lra


def test_loudness_range_is_unmeasured_below_one_window():
    assert analysis.loudness_range(np.zeros(SR), SR) is None


def test_spectrum_averages_the_whole_file_not_just_the_centre():
    """A whole-file Hann window hid a burst at the start by ~39 dB."""
    n = SR * 10
    t = np.arange(n) / SR
    base = 0.1 * np.sin(2 * np.pi * 440 * t)
    burst = 0.05 * np.sin(2 * np.pi * 10000 * np.arange(SR // 2) / SR)
    at_start, in_middle = base.copy(), base.copy()
    at_start[: len(burst)] += burst
    in_middle[n // 2: n // 2 + len(burst)] += burst
    a = analysis.bands_db_rel(at_start, SR)["high"]
    b = analysis.bands_db_rel(in_middle, SR)["high"]
    assert abs(a - b) < 2.0, (a, b)


def test_jev_facts_have_no_absolute_paths():
    import engineer_server
    facts = analysis.analyze_master(_wav(_sine(440, 0.3)))
    assert os.path.isabs(facts["file"])                 # local API keeps it
    safe = engineer_server._jev_facts(facts)
    assert safe["file"] == "t.wav" and "/" not in safe["file"]

    stems = analysis.analyze_stems([_wav(_sine(300, 0.3), name="01_vocals.wav")])
    safe_stems = engineer_server._jev_facts(stems)
    assert safe_stems["stems"][0]["path"] == "01_vocals.wav"
    assert "band_overlap_pairs" in stems and "masking_pairs" not in stems


# ---------------------------------------------------------------------------
# formats, channel layouts and measured edge cases
# ---------------------------------------------------------------------------
def test_float_wav_is_decoded_at_its_real_level():
    """Decoding a float WAV as int32 turns a 0.5 tone into near-silence."""
    tone = _sine(1000, 0.5)
    for bits in (32, 64):
        facts = analysis.analyze_master(_wav_float(tone, bits=bits))
        assert facts["bit_depth"] == bits, facts
        assert abs(facts["loudness"]["sample_peak_dbfs"] - (-6.02)) < 0.05, (bits, facts)


def test_extensible_float_wav_is_decoded():
    """WAVE_FORMAT_EXTENSIBLE + IEEE_FLOAT subtype is what many float bounces use."""
    facts = analysis.analyze_master(_wav_float(_sine(1000, 0.5), bits=32, extensible=True))
    assert facts["bit_depth"] == 32, facts
    assert abs(facts["loudness"]["sample_peak_dbfs"] - (-6.02)) < 0.05, facts


def test_non_finite_float_samples_are_refused_with_context():
    tone = _sine(1000, 0.5, seconds=1.0)
    tone[100] = np.nan
    path = _wav_float(tone)
    try:
        analysis.load(path)
    except ValueError as exc:
        assert path in str(exc) and "non-finite" in str(exc), str(exc)
    else:
        raise AssertionError("a NaN sample must not reach the metrics")


def test_five_one_uses_surround_weights_and_excludes_lfe():
    t = np.arange(SR * 4) / SR
    tone = 0.1 * np.sqrt(2) * np.sin(2 * np.pi * 997 * t)
    zeros = np.zeros_like(tone)
    front = np.stack([tone, tone, tone, zeros, zeros, zeros], axis=1)
    lfe_only = np.stack([zeros, zeros, zeros, tone, zeros, zeros], axis=1)
    assert analysis.channel_layout(6) == "5.1"
    # three unit-weight channels add power (+4.77 dB over one channel).
    assert abs(analysis.integrated_lufs(front, SR) - (-15.23)) < 0.5
    assert analysis.integrated_lufs(lfe_only, SR) <= -60.0


def test_unsupported_channel_counts_are_refused():
    for channels in (3, 4, 8):
        try:
            analysis.channel_layout(channels)
        except ValueError as exc:
            assert f"{channels} channels" in str(exc), str(exc)
        else:
            raise AssertionError(f"{channels} channels must be refused")
    path = _wav(np.zeros((SR, 4)))
    try:
        analysis.load(path)
    except ValueError as exc:
        assert "4 channels" in str(exc), str(exc)
    else:
        raise AssertionError("a 4-channel WAV must be refused")


def test_stereo_crest_uses_all_channel_energy():
    """The old mono-RMS denominator read an anti-phase file as ~220 dB crest."""
    tone = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(SR * 3) / SR)
    facts = analysis.analyze_master(_wav(np.stack([tone, -tone], axis=1)))
    assert abs(facts["loudness"]["crest_db"] - 3.01) < 0.15, facts["loudness"]


def test_stereo_spectrum_averages_power_not_the_fold_down():
    """An anti-phase pair cancels in mono but has the same spectrum as one channel."""
    tone = _sine(997, 0.2)
    mono = analysis.bands_db_rel(tone, SR)
    anti = analysis.bands_db_rel(np.stack([tone, -tone], axis=1), SR)
    assert mono == anti, (mono, anti)


def test_flat_top_at_minus_one_dbfs_is_not_sample_clipping():
    """A ceiling below full scale has no 0.999 hits but still has flat tops."""
    clipped = np.clip(1.2 * np.sin(2 * np.pi * 440 * np.arange(SR * 3) / SR),
                      -0.891, 0.891)
    facts = analysis.clipping(clipped)
    assert facts["count"] == 0, facts
    assert facts["flat_top_frames"] > 1000, facts


def test_measured_cutoff_reports_the_edge_and_full_band_is_none():
    rng = np.random.default_rng(7)
    full = rng.normal(size=SR * 10)
    spectrum = np.fft.rfft(full)
    freqs = np.fft.rfftfreq(len(full), 1 / SR)
    spectrum[freqs > 20000] = 0
    cut = np.fft.irfft(spectrum, n=len(full))
    measured = analysis.spectral_cutoff(cut, SR)
    assert measured["cutoff_hz"] == 20000, measured
    assert measured["lossy_suspected"] is True, measured
    assert analysis.spectral_cutoff(full, SR)["cutoff_hz"] is None


def test_partial_section_tail_is_included_and_marked():
    signal = 0.2 * np.sin(2 * np.pi * 220 * np.arange(int(23 * SR)) / SR)
    facts = analysis.analyze_sections(_wav(signal), window_s=6.0)
    last = facts["windows"][-1]
    assert last["partial"] is True and last["duration_s"] == 5.0, last
    assert facts["tail_dropped_s"] == 0.0, facts


def test_temporal_overlap_separates_never_from_always_coactive():
    t = np.arange(SR * 6) / SR
    tone = 0.4 * np.sin(2 * np.pi * 500 * t)
    never = analysis.analyze_stems([
        _wav(np.where((t >= 0) & (t < 2), tone, 0.0)),
        _wav(np.where((t >= 4) & (t < 6), tone, 0.0)),
    ])["band_overlap_pairs"][0]
    always = analysis.analyze_stems([_wav(tone), _wav(tone)])["band_overlap_pairs"][0]
    assert never["band_overlap_db"] is None and never["coactive_fraction"] == 0.0, never
    assert always["coactive_fraction"] > 0.9 and always["dominant_band"] == "mid", always


def test_reference_measurement_is_idempotent():
    path = _wav(0.2 * np.sin(2 * np.pi * 500 * np.arange(SR * 5) / SR))
    assert analysis.analyze_reference(path) == analysis.analyze_reference(path)


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
