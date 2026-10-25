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


def load(path: str) -> tuple[np.ndarray, int]:
    """Return (samples[frames, channels] float32 in [-1,1], sample_rate)."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"audio file not found: {path}")
    if path.lower().endswith(".wav"):
        with wave.open(path) as w:
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            raw = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
        return raw.reshape(-1, ch), sr
    try:
        import soundfile as sf  # optional
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install soundfile to read this format") from exc
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data, sr


def _mono(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1) if x.ndim == 2 and x.shape[1] > 1 else x.reshape(-1)


def bands_db_rel(mono: np.ndarray, sr: int) -> dict[str, float]:
    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
    freqs = np.fft.rfftfreq(len(mono), 1 / sr)
    total = float(spec.sum()) + 1e-12
    return {name: round(10 * np.log10(float(spec[(freqs >= lo) & (freqs < hi)].sum()) / total + 1e-12), 1)
            for name, (lo, hi) in BANDS.items()}


def integrated_lufs(mono: np.ndarray, sr: int) -> float:
    """BS.1770 integrated loudness: K-weighting + 400 ms gating (absolute and relative).

    The gating matters: without it, silence and quiet intros drag the number down and
    the same master reads differently depending on how much air it has at the edges.
    """
    try:
        import pyloudnorm as pyln  # exact, optional
        return round(float(pyln.Meter(sr).integrated_loudness(mono)), 2)
    except Exception:
        pass

    y = _k_weighted(mono, sr)
    block = int(0.4 * sr)
    if len(y) < block:
        return round(-0.691 + 10 * np.log10(float(np.mean(y ** 2)) + 1e-20), 2)

    step = max(1, block // 4)
    starts = range(0, len(y) - block + 1, step)
    powers = np.array([float(np.mean(y[i:i + block] ** 2)) for i in starts]) + 1e-20
    block_lufs = -0.691 + 10 * np.log10(powers)

    gated = powers[block_lufs > -70.0]                      # absolute gate
    if gated.size == 0:
        return -70.0
    relative = -0.691 + 10 * np.log10(float(np.mean(gated))) - 10.0
    keep = gated[(-0.691 + 10 * np.log10(gated)) > relative]  # relative gate
    if keep.size == 0:
        keep = gated
    return round(-0.691 + 10 * np.log10(float(np.mean(keep))), 2)


def _k_weighted(mono: np.ndarray, sr: int) -> np.ndarray:
    """K-weighting in the frequency domain (same magnitude response as the biquads)."""
    x = mono.astype(np.float64)
    n = len(x)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    z = np.exp(-2j * np.pi * freqs / sr)
    z2 = z * z

    def response(b, a):
        return (b[0] + b[1] * z + b[2] * z2) / (a[0] + a[1] * z + a[2] * z2)

    h = response(np.array([1.53512485958697, -2.69169618940638, 1.19839281085285]),
                 np.array([1.0, -1.69065929318241, 0.73248077421585]))
    h *= response(np.array([1.0, -2.0, 1.0]),
                  np.array([1.0, -1.99004745483398, 0.99007225036621]))
    return np.fft.irfft(spec * h, n=n)


def true_peak_dbtp(mono: np.ndarray, sr: int, oversample: int = 4) -> float:
    """4x oversampled peak via FFT zero-padding, processed in chunks."""
    peak = 0.0
    chunk = 1 << 17
    for start in range(0, len(mono), chunk):
        seg = mono[start:start + chunk]
        if len(seg) < 16:
            continue
        n = len(seg)
        spec = np.fft.rfft(seg)
        up = np.fft.irfft(spec, n=n * oversample) * oversample
        peak = max(peak, float(np.max(np.abs(up))))
    return round(20 * np.log10(peak + 1e-12), 2)


def loudness_range(mono: np.ndarray, sr: int, win_s: float = 3.0) -> float:
    win = int(win_s * sr)
    if len(mono) <= win:
        return 0.0
    step = max(1, win // 2)
    vals = []
    for start in range(0, len(mono) - win, step):
        seg = mono[start:start + win]
        vals.append(20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12))
    if not vals:
        return 0.0
    return round(float(np.percentile(vals, 95) - np.percentile(vals, 10)), 2)


def stereo_correlation(stereo: np.ndarray) -> float:
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return 1.0
    l, r = stereo[:, 0], stereo[:, 1]
    denom = np.std(l) * np.std(r)
    if denom < 1e-9:
        return 1.0
    return round(float(np.corrcoef(l, r)[0, 1]), 3)


def clipping(stereo: np.ndarray, thr: float = 0.999) -> dict:
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
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)

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
        "loudness": {
            "integrated_lufs": integrated_lufs(mono, sr),
            "loudness_range_lu": loudness_range(mono, sr),
            "true_peak_dbtp": true_peak_dbtp(mono, sr),
            "sample_peak_dbfs": round(20 * np.log10(peak + 1e-12), 2),
            "crest_db": round(20 * np.log10(peak / rms + 1e-12), 2),
        },
        "spectral": {
            "bands_db_rel": bands_db_rel(mono, sr),
            "centroid_hz": _centroid(mono, sr),
        },
        "stereo": {"phase_correlation": stereo_correlation(data)},
        "clipping": clipping(data),
        "noise": {"floor_dbfs": noise_floor_dbfs(mono, sr)},
        "bandwidth": spectral_cutoff(mono, sr),
    }
    return plain(stats)


def _centroid(mono: np.ndarray, sr: int) -> int:
    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
    freqs = np.fft.rfftfreq(len(mono), 1 / sr)
    return int(float((freqs * spec).sum() / (spec.sum() + 1e-12)))


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
        "integrated_lufs": integrated_lufs(mono, sr),
        "true_peak_dbtp": true_peak_dbtp(mono, sr),
        "loudness_range_lu": loudness_range(mono, sr),
        "phase_correlation": stereo_correlation(data),
    })


def analyze_sections(path: str, window_s: float = 30.0) -> dict:
    """Per-window analysis. A hot chorus disappears in a whole-file average."""
    data, sr = load(path)
    mono = _mono(data)
    win = int(window_s * sr)
    if len(mono) < win:
        return {"window_s": window_s, "windows": [],
                "note": "file shorter than one window; use analyze_master"}
    windows = []
    for start in range(0, len(mono) - win + 1, win):
        seg, chunk = mono[start:start + win], data[start:start + win]
        windows.append({"start_s": round(start / sr, 1),
                        "integrated_lufs": integrated_lufs(seg, sr),
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
    return plain({"window_s": window_s, "windows": windows,
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
            "peak_dbfs": round(20 * np.log10(np.max(np.abs(mono)) + 1e-12), 2),
            "bands_db_rel": bands_db_rel(mono, sr),
            "low_correlation": low_band_correlation(data, sr),
        })
    masking = []
    for i in range(len(stems)):
        for j in range(i + 1, len(stems)):
            a, b = stems[i], stems[j]
            overlap = sum(min(a["bands_db_rel"][k], b["bands_db_rel"][k])
                          for k in BANDS) / len(BANDS)
            masking.append({"a": a["name"], "b": b["name"], "band_overlap_db": round(overlap, 2)})
    masking.sort(key=lambda m: -m["band_overlap_db"])
    return plain({"stems": stems, "masking_pairs": masking[:5],
                  "gates": stem_gates(stems)})


VOCAL_HINTS = ("vocal", "vox", "voice", "lead", "voz")


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
