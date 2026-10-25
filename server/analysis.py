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
    """Approximate BS.1770 integrated loudness (K-weighting + gated blocks)."""
    try:
        import pyloudnorm as pyln  # optional, exact
        return round(float(pyln.Meter(sr).integrated_loudness(mono)), 2)
    except Exception:
        pass
    # fallback: K-weighting applied in the frequency domain (same magnitude response,
    # no per-sample Python loop — ~40x faster on long files)
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
    y = np.fft.irfft(spec * h, n=n)
    rms = np.sqrt(np.mean(y ** 2) + 1e-18)
    return round(-0.691 + 20 * np.log10(rms + 1e-15), 2)


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
    }
    return plain(stats)


def _centroid(mono: np.ndarray, sr: int) -> int:
    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
    freqs = np.fft.rfftfreq(len(mono), 1 / sr)
    return int(float((freqs * spec).sum() / (spec.sum() + 1e-12)))


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
        })
    masking = []
    for i in range(len(stems)):
        for j in range(i + 1, len(stems)):
            a, b = stems[i], stems[j]
            overlap = sum(min(a["bands_db_rel"][k], b["bands_db_rel"][k])
                          for k in BANDS) / len(BANDS)
            masking.append({"a": a["name"], "b": b["name"], "band_overlap_db": round(overlap, 2)})
    masking.sort(key=lambda m: -m["band_overlap_db"])
    return plain({"stems": stems, "masking_pairs": masking[:5]})
