"""Audio measurement. Deterministic, offline, no AI.

WAV via the stdlib; FLAC/OGG/AIFF via soundfile if installed.
"""
from __future__ import annotations

import os
import wave

import numpy as np

def plain(obj):
    """Recursively convert numpy scalars so json.dumps never sees np.float64."""
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


BANDS = {
    "sub": (20, 60), "bass": (60, 250), "low_mid": (250, 500),
    "mid": (500, 2000), "high_mid": (2000, 6000), "high": (6000, 16000),
}

# Sibilance is measured separately: it is not a seventh band of the reference curve.
SIBILANCE_BAND = (5000.0, 9000.0)


def load(path: str) -> tuple[np.ndarray, int]:
    """Return (samples[frames, channels] float32 in [-1,1], sample_rate).

    Reads 8/16/24/32-bit integer WAV and defers float WAV to soundfile. A Logic
    bounce is 24-bit by default, so assuming 16-bit silently produced garbage.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"audio file not found: {path}")

    if path.lower().endswith(".wav"):
        try:
            with wave.open(path) as w:
                sr, ch, n, width = (w.getframerate(), w.getnchannels(),
                                    w.getnframes(), w.getsampwidth())
                raw = w.readframes(n)
        except wave.Error:
            return _load_via_soundfile(path)
        if width == 2:
            x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        elif width == 3:
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - 0x1000000, v)
            x = v.astype(np.float32) / 8388608.0
        elif width == 4:
            x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
        elif width == 1:
            x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            raise ValueError(f"unsupported WAV sample width: {width} bytes")
        return x.reshape(-1, ch), sr

    return _load_via_soundfile(path)


def _load_via_soundfile(path: str) -> tuple[np.ndarray, int]:
    try:
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install soundfile to read this format (float WAV, FLAC, OGG, AIFF)") from exc
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data, sr


def _mono(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1) if x.ndim == 2 and x.shape[1] > 1 else x.reshape(-1)


def _welch_psd(mono: np.ndarray, sr: int, nperseg: int = 8192) -> tuple[np.ndarray, np.ndarray]:
    """Averaged periodogram (Welch, 50 % overlap, Hann).

    One FFT over the whole file weights its centre and nearly ignores intro/outro.
    Welch averages every 8192-sample segment with 50 % overlap, so a loud section
    is not swallowed by the analysis window.
    """
    x = np.asarray(mono, dtype=np.float64).reshape(-1)
    n = len(x)
    if n < 2:
        return np.zeros(1), np.zeros(1)
    seg = min(nperseg, n)
    window = np.hanning(seg)
    step = max(1, seg // 2)
    starts = range(0, n - seg + 1, step) if n >= seg else [0]
    acc = np.zeros(seg // 2 + 1)
    count = 0
    for start in starts:
        chunk = x[start:start + seg]
        if len(chunk) < seg:
            chunk = np.pad(chunk, (0, seg - len(chunk)))
        acc += np.abs(np.fft.rfft(chunk * window)) ** 2
        count += 1
    return np.fft.rfftfreq(seg, 1 / sr), acc / max(1, count)


def bands_db_rel(mono: np.ndarray, sr: int,
                 bands: dict[str, tuple[float, float]] | None = None) -> dict[str, float]:
    bands = BANDS if bands is None else bands
    freqs, psd = _welch_psd(mono, sr)
    total = float(psd.sum()) + 1e-12
    return {name: round(10 * np.log10(float(psd[(freqs >= lo) & (freqs < hi)].sum()) / total + 1e-12), 1)
            for name, (lo, hi) in bands.items()}


def sibilance_db_rel(mono: np.ndarray, sr: int) -> float:
    """5-9 kHz energy relative to full-band energy (the band a de-esser acts on)."""
    return bands_db_rel(mono, sr, {"sibilance": SIBILANCE_BAND})["sibilance"]


def integrated_lufs(data, sr: int) -> float:
    """BS.1770 integrated loudness, summed across channels.

    Channel powers add (L^2 + R^2), they do not average: averaging a stereo signal
    under-reports by ~3 LU, and a hard out-of-phase pair reads silence.
    """
    x = np.atleast_2d(data if data.ndim > 1 else data.reshape(-1, 1))
    try:
        import pyloudnorm as pyln  # exact, optional
        exact = float(pyln.Meter(sr).integrated_loudness(x))
        if np.isfinite(exact):
            return round(exact, 2)
    except Exception:
        pass

    n = x.shape[0]
    block = int(0.4 * sr)
    step = max(1, block // 4)
    if n < block:
        powers = [float(np.sum(np.mean(_k_weighted(x[:, c], sr) ** 2)))
                  for c in range(x.shape[1])]
        return round(-0.691 + 10 * np.log10(sum(powers) + 1e-20), 2)

    weighted = [_k_weighted(x[:, c], sr) for c in range(x.shape[1])]
    starts = range(0, n - block + 1, step)
    powers = np.array([sum(float(np.mean(w[i:i + block] ** 2)) for w in weighted)
                       for i in starts]) + 1e-20
    block_lufs = -0.691 + 10 * np.log10(powers)

    gated = powers[block_lufs > -70.0]
    if gated.size == 0:
        return -70.0
    relative = -0.691 + 10 * np.log10(float(np.mean(gated))) - 10.0
    keep = gated[(-0.691 + 10 * np.log10(gated)) > relative]
    if keep.size == 0:
        keep = gated
    return round(-0.691 + 10 * np.log10(float(np.mean(keep))), 2)


def _k_coefficients(sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """BS.1770 K-weighting biquads derived for the actual sample rate.

    The tabulated coefficients are only valid at 48 kHz. Reusing them at 44.1 or
    96 kHz, as this module used to do, moved the shelf and misread loudness by up
    to ~1.7 LU. These formulas are the ITU parameterisation (same response as the
    published 48 kHz constants).
    """
    # Stage 1 — high-shelf (head-related filter).
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / sr)
    vh = 10.0 ** (gain / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    b1 = np.array([(vh + vb * k / q + k * k) / a0,
                   2.0 * (k * k - vh) / a0,
                   (vh - vb * k / q + k * k) / a0])
    a1 = np.array([1.0,
                   2.0 * (k * k - 1.0) / a0,
                   (1.0 - k / q + k * k) / a0])
    # Stage 2 — 38 Hz high-pass.
    f0, q = 38.13547087602444, 0.5003270373238773
    k = np.tan(np.pi * f0 / sr)
    den = 1.0 + k / q + k * k
    b2 = np.array([1.0, -2.0, 1.0])
    a2 = np.array([1.0, 2.0 * (k * k - 1.0) / den, (1.0 - k / q + k * k) / den])
    return b1, a1, b2, a2


def _k_weighted(mono: np.ndarray, sr: int) -> np.ndarray:
    """K-weighting in the frequency domain (same magnitude response as the biquads)."""
    x = mono.astype(np.float64)
    n = len(x)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    z = np.exp(-2j * np.pi * freqs / sr)
    z2 = z * z
    b1, a1, b2, a2 = _k_coefficients(sr)

    def response(b, a):
        return (b[0] + b[1] * z + b[2] * z2) / (a[0] + a[1] * z + a[2] * z2)

    h = response(b1, a1) * response(b2, a2)
    return np.fft.irfft(spec * h, n=n)


def _interp_phases(oversample: int = 4, half_taps: int = 8) -> list[np.ndarray]:
    """Windowed-sinc polyphase filters for `oversample`x interpolation.

    Each phase has unity DC gain, so a constant reads itself. Eight zero crossings
    each side gives ~80 dB image rejection with 65 taps.
    """
    n_taps = 2 * half_taps * oversample + 1
    t = np.arange(n_taps) - (n_taps - 1) / 2.0
    h = np.sinc(t / oversample) * np.kaiser(n_taps, 8.0)
    return [h[p::oversample] / h[p::oversample].sum() for p in range(oversample)]


def _true_peak_channel(ch: np.ndarray, phases: list[np.ndarray]) -> float:
    """Peak of one channel after polyphase interpolation, boundaries included.

    Convolving the *whole* channel (not central slices of blocks) is what makes
    the first and last samples measurable: the old block version discarded a
    quarter block at each edge, so an impulse at the start read -76 dBTP and one
    at the end read -240 dBTP.
    """
    peak = float(np.max(np.abs(ch))) if ch.size else 0.0
    # Phase 0 is the signal itself (the sample peak above already covers it);
    # only the fractional-delay phases can reveal an inter-sample peak.
    for phase in phases[1:]:
        y = np.convolve(ch, phase, mode="full")
        peak = max(peak, float(np.max(np.abs(y))))
    return peak


def true_peak_dbtp(data, sr: int, oversample: int = 4) -> float:
    """Inter-sample true peak per BS.1770 (4x minimum), summed across channels by max."""
    x = np.atleast_2d(data if data.ndim > 1 else data.reshape(-1, 1)).astype(np.float64)
    phases = _interp_phases(oversample)
    peak = max(_true_peak_channel(x[:, c], phases) for c in range(x.shape[1]))
    return round(20 * np.log10(peak + 1e-12), 2)


def loudness_range(data, sr: int, win_s: float = 3.0, hop_s: float = 0.1) -> float | None:
    """EBU R128 loudness range (LU), per EBU Tech 3342.

    K-weighted 3 s short-term loudness every 100 ms, absolute gate at -70 LUFS,
    relative gate 20 LU below the gated mean, then P95 - P10 of what survives.

    The previous implementation used unweighted RMS windows with no gating and
    its own percentiles: a metric that does not mean what `loudness_range_lu`
    means to every other tool. Returns None when the file is shorter than one
    window (the range is not measurable, not zero).
    """
    x = np.atleast_2d(data if data.ndim > 1 else data.reshape(-1, 1)).astype(np.float64)
    n = x.shape[0]
    win = int(round(win_s * sr))
    if n < win:
        return None
    hop = max(1, int(round(hop_s * sr)))
    power = np.zeros(n)
    for c in range(x.shape[1]):
        power += _k_weighted(x[:, c], sr) ** 2
    cumulative = np.concatenate(([0.0], np.cumsum(power)))
    starts = np.arange(0, n - win + 1, hop)
    means = (cumulative[starts + win] - cumulative[starts]) / win
    short_term = -0.691 + 10.0 * np.log10(means + 1e-20)

    keep = short_term > -70.0                    # absolute gate
    if not keep.any():
        return 0.0
    gated_means, gated_lufs = means[keep], short_term[keep]
    relative = -0.691 + 10.0 * np.log10(float(np.mean(gated_means))) - 20.0
    gated_lufs = gated_lufs[gated_lufs > relative]  # relative gate
    if gated_lufs.size == 0:
        return 0.0
    return round(float(np.percentile(gated_lufs, 95) - np.percentile(gated_lufs, 10)), 2)


def stereo_correlation(stereo: np.ndarray) -> float:
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return 1.0
    l, r = stereo[:, 0], stereo[:, 1]
    denom = np.std(l) * np.std(r)
    if denom < 1e-9:
        return 1.0
    return round(float(np.corrcoef(l, r)[0, 1]), 3)


def mono_cancel_db(stereo: np.ndarray) -> float:
    """How much level survives a mono fold-down, in dB. -inf means it cancels."""
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return 0.0
    mid = stereo.mean(axis=1)
    worst = max(float(np.sqrt(np.mean(stereo[:, c] ** 2))) for c in range(stereo.shape[1]))
    if worst < 1e-9:
        return 0.0
    return round(float(20 * np.log10(float(np.sqrt(np.mean(mid ** 2))) / worst + 1e-12)), 1)


def clipping(stereo: np.ndarray, thr: float = 0.999) -> dict:
    """Clipped FRAMES, not channel samples.

    A frame counts once even when both channels clip, so the proportion gate's
    denominator is duration * sample_rate, independent of channel count.
    """
    hits = np.abs(stereo) >= thr
    return {"count": int(hits.any(axis=1).sum()) if hits.ndim > 1 else int(hits.sum())}


def noise_floor_dbfs(mono: np.ndarray, sr: int, win_s: float = 0.1):
    """Noise floor measured **only in silence**. Returns None when there is no silence.

    On continuous music the quietest windows are soft passages, not noise: reporting them
    as a floor would flag every track. Without a gap (or stems) the floor is unmeasurable.
    """
    win = max(1, int(win_s * sr))
    if len(mono) < win * 4:
        return None
    rms = np.array([float(np.sqrt(np.mean(mono[i:i + win] ** 2)))
                    for i in range(0, len(mono) - win, win)])
    if rms.size == 0:
        return None
    db = 20 * np.log10(rms + 1e-12)
    quietest, median = float(np.percentile(db, 2)), float(np.percentile(db, 50))
    if median - quietest < 20.0:   # no real gap: the quiet windows are just soft music
        return None
    in_silence = db[db <= quietest + 6.0]
    return round(float(np.percentile(in_silence, 90)), 1) if in_silence.size else None


def spectral_cutoff(mono: np.ndarray, sr: int) -> dict:
    """Detect a lossy-codec cliff: energy collapsing just above 16 kHz."""
    nyquist = sr / 2
    if nyquist < 20000:
        return {"cutoff_hz": round(nyquist), "drop_db": 0.0, "lossy_suspected": False,
                "note": "sample rate too low to test the 14-18 kHz region"}
    seg = mono[: sr * 60] if len(mono) > sr * 60 else mono
    freqs, spec = _welch_psd(seg, sr)

    def band_db(lo, hi):
        total = float(spec.sum()) + 1e-12
        return 10 * np.log10(float(spec[(freqs >= lo) & (freqs < hi)].sum()) / total + 1e-12)

    below, above = band_db(14000, 16000), band_db(16000, 18000)
    drop = round(below - above, 1)
    return {"cutoff_hz": 16000, "drop_db": drop, "lossy_suspected": bool(drop > 20.0)}


def analyze_master(path: str) -> dict:
    data, sr = load(path)
    mono = _mono(data)
    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(mono ** 2)) + 1e-12)
    stats = {
        "file": os.path.abspath(path),
        "sample_rate": sr,
        "channels": int(data.shape[1]) if data.ndim == 2 else 1,
        "duration_seconds": round(len(mono) / sr, 2),
        "bit_depth": _wav_bit_depth(path),
        "loudness": {
            "integrated_lufs": integrated_lufs(data, sr),
            "loudness_range_lu": loudness_range(data, sr),
            "true_peak_dbtp": true_peak_dbtp(data, sr),
            "sample_peak_dbfs": round(20 * np.log10(peak + 1e-12), 2),
            "crest_db": round(20 * np.log10(peak / rms + 1e-12), 2),
        },
        "spectral": {
            "bands_db_rel": bands_db_rel(mono, sr),
            "sibilance_db_rel": sibilance_db_rel(mono, sr),
            "centroid_hz": _centroid(mono, sr),
        },
        "stereo": {"phase_correlation": stereo_correlation(data),
                    "mono_cancel_db": mono_cancel_db(data)},
        "clipping": clipping(data),
        "noise": {"floor_dbfs": noise_floor_dbfs(mono, sr)},
        "bandwidth": spectral_cutoff(mono, sr),
    }
    return plain(stats)


def _wav_bit_depth(path: str) -> int | None:
    if not path.lower().endswith(".wav"):
        return None
    try:
        with wave.open(path) as w:
            return w.getsampwidth() * 8
    except wave.Error:
        return None


def _centroid(mono: np.ndarray, sr: int) -> int:
    freqs, psd = _welch_psd(mono, sr)
    return int(float((freqs * psd).sum() / (psd.sum() + 1e-12)))


def low_band_correlation(stereo: np.ndarray, sr: int, cutoff_hz: float = 120.0) -> float:
    """Mono-bass check: L/R correlation below the cutoff, where a stereo bass collapses."""
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return 1.0
    l, r = stereo[:, 0], stereo[:, 1]
    spec_l, spec_r = np.fft.rfft(l), np.fft.rfft(r)
    freqs = np.fft.rfftfreq(len(l), 1 / sr)
    mask = freqs <= cutoff_hz
    if not mask.any():
        return 1.0
    lo = np.fft.irfft(spec_l * mask, n=len(l))
    ro = np.fft.irfft(spec_r * mask, n=len(r))
    denom = np.std(lo) * np.std(ro)
    if denom < 1e-9:
        return 1.0
    return round(float(np.corrcoef(lo, ro)[0, 1]), 3)


def analyze_reference(path: str) -> dict:
    """Measure a commercial reference: its band balance becomes the reference curve."""
    data, sr = load(path)
    mono = _mono(data)
    return plain({
        "file": os.path.abspath(path),
        "reference_curve": bands_db_rel(mono, sr),
        "sibilance_db_rel": sibilance_db_rel(mono, sr),
        # Loudness and peaks are summed across channels, not read from the mono
        # fold-down: that under-reported a correlated stereo reference by ~3 LU.
        "integrated_lufs": integrated_lufs(data, sr),
        "true_peak_dbtp": true_peak_dbtp(data, sr),
        "loudness_range_lu": loudness_range(data, sr),
        "phase_correlation": stereo_correlation(data),
    })


def analyze_sections(path: str, window_s: float = 30.0) -> dict:
    """Per-window analysis. A hot chorus disappears in a whole-file average."""
    data, sr = load(path)
    mono = _mono(data)
    win = int(window_s * sr)
    if len(mono) < win:
        return {"window_s": window_s, "windows": [], "reliable": False,
                "note": "file shorter than one window; use analyze_master"}
    windows = []
    for start in range(0, len(mono) - win + 1, win):
        seg, chunk = mono[start:start + win], data[start:start + win]
        windows.append({"start_s": round(start / sr, 1),
                        # measure the stereo window: a mono fold-down reads ~3 LU low
                        "integrated_lufs": integrated_lufs(chunk, sr),
                        "bands_db_rel": bands_db_rel(seg, sr),
                        "phase_correlation": stereo_correlation(chunk)})
    median_lufs = float(np.median([w["integrated_lufs"] for w in windows]))
    median_bands = {b: float(np.median([w["bands_db_rel"][b] for w in windows])) for b in BANDS}
    tilt = [w["bands_db_rel"]["high_mid"] - w["bands_db_rel"]["bass"] for w in windows]
    median_tilt = float(np.median(tilt))
    for w, t in zip(windows, tilt):
        w["deviation_db"] = {b: round(w["bands_db_rel"][b] - median_bands[b], 1) for b in BANDS}
        w["lufs_delta"] = round(w["integrated_lufs"] - median_lufs, 1)
        w["tilt"] = round(t, 1)
        w["tilt_delta"] = round(t - median_tilt, 1)
        # A single robust criterion: loudness or spectral tilt moving away from the file median.
        w["flagged"] = abs(w["lufs_delta"]) > 3.0 or abs(w["tilt_delta"]) > 3.0
    reliable = len(windows) >= 3
    if not reliable:                      # a median of two points is degenerate
        for w in windows:
            w["flagged"] = False
    return plain({"window_s": window_s, "windows": windows, "reliable": reliable,
                  "median_lufs": round(median_lufs, 2),
                  "median_bands_db_rel": {k: round(v, 1) for k, v in median_bands.items()},
                  "median_tilt": round(median_tilt, 1),
                  "flagged_windows": [w["start_s"] for w in windows if w["flagged"]]})


def analyze_stems(paths: list[str]) -> dict:
    """Per-stem measurements plus a roughness matrix between stems."""
    stems = []
    for p in paths:
        data, sr = load(p)
        mono = _mono(data)
        stems.append({
            "name": os.path.splitext(os.path.basename(p))[0],
            "path": os.path.abspath(p),
            "rms_dbfs": round(20 * np.log10(np.sqrt(np.mean(mono ** 2)) + 1e-12), 2),
            "integrated_lufs": integrated_lufs(data, sr),
            "phase_correlation": stereo_correlation(data),
            "low_instrument": bool(bands_db_rel(mono, sr)["bass"] > -12.0),
            "peak_dbfs": round(20 * np.log10(np.max(np.abs(mono)) + 1e-12), 2),
            "bands_db_rel": bands_db_rel(mono, sr),
            "sibilance_db_rel": sibilance_db_rel(mono, sr),
            "low_correlation": low_band_correlation(data, sr),
        })
    overlap_pairs = []
    for i in range(len(stems)):
        for j in range(i + 1, len(stems)):
            a, b = stems[i], stems[j]
            overlap = sum(min(a["bands_db_rel"][k], b["bands_db_rel"][k])
                          for k in BANDS) / len(BANDS)
            overlap_pairs.append({"a": a["name"], "b": b["name"],
                                  "band_overlap_db": round(overlap, 2)})
    overlap_pairs.sort(key=lambda m: -m["band_overlap_db"])
    return plain({"stems": stems,
                  "band_overlap_pairs": overlap_pairs[:5],
                  "band_overlap_note": "mean per-band minimum of relative band energy; "
                                       "a spectral-crowding proxy, not perceptual masking",
                  "gates": stem_gates(stems)})


VOCAL_HINTS = ("vocal", "vox", "voice", "voz")


def stem_gates(stems: list[dict]) -> list[dict]:
    """Conditions that need a mix fix, measured on the exported stems."""
    out: list[dict] = []
    vocals = [s for s in stems if any(h in s["name"].lower() for h in VOCAL_HINTS)]
    rest = [s for s in stems if s not in vocals]
    if vocals and rest:
        v = 10 ** (vocals[0]["rms_dbfs"] / 10)
        others = sum(10 ** (s["rms_dbfs"] / 10) for s in rest)
        relative = round(float(10 * np.log10(v / (others + 1e-12) + 1e-12)), 1)
        if relative < -12.0:
            out.append({"gate": "vocal_buried", "stem": vocals[0]["name"],
                        "relative_db": relative})

    peaks = [s["peak_dbfs"] for s in stems]
    if len(peaks) >= 3:
        excess = round(float(max(peaks) - np.median(peaks)), 1)
        if excess > 6.0:
            loudest = stems[int(np.argmax(peaks))]
            out.append({"gate": "single_element_peak", "stem": loudest["name"],
                        "excess_db": excess})

    for s in stems:
        if s["low_correlation"] < 0.3:
            out.append({"gate": "mono_bass", "stem": s["name"],
                        "low_correlation": s["low_correlation"]})
    return out
