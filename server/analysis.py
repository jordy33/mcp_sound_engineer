"""Audio measurement. Deterministic, offline, no AI.

WAV via the stdlib (PCM and IEEE float); FLAC/OGG/AIFF via soundfile if installed.
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

# BS.1770 channel weights. The standard WAV 5.1 order is FL/FR/FC/LFE/BL/BR:
# surrounds are +1.5 dB and the LFE channel is excluded from the loudness sum.
_SURROUND_GAIN = 1.41          # 10 ** (1.5 / 20)
_FIVE_ONE_WEIGHTS = np.array([1.0, 1.0, 1.0, 0.0, _SURROUND_GAIN, _SURROUND_GAIN])


def channel_layout(channels: int) -> str:
    """Name a supported layout, or refuse an unsupported one explicitly."""
    if channels == 1:
        return "mono"
    if channels == 2:
        return "stereo"
    if channels == 6:
        return "5.1"
    raise ValueError(f"{channels} channels: supported layouts are mono, stereo and 5.1 "
                     f"(standard WAV order FL/FR/FC/LFE/BL/BR)")


def _channel_weights(channels: int) -> np.ndarray:
    if channels == 1:
        return np.ones(1)
    if channels == 2:
        return np.ones(2)
    if channels == 6:
        return _FIVE_ONE_WEIGHTS.copy()
    raise ValueError(f"{channels} channels: supported layouts are mono, stereo and 5.1")


def _wav_fmt(path: str) -> tuple[int, int, int] | None:
    """Return (RIFF format code, channels, bits) from the WAV `fmt ` chunk.

    `wave` does not expose the format code, and Python 3.15 starts accepting
    IEEE-float WAVs there; decoding those as int32 silently corrupts the data.
    Reading the chunk ourselves is the only portable way to tell them apart.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
            if (len(header) < 12 or header[:4] != b"RIFF"
                    or header[8:12] != b"WAVE"):
                return None
            while True:
                chunk = handle.read(8)
                if len(chunk) < 8:
                    return None
                chunk_id = chunk[:4]
                size = int.from_bytes(chunk[4:8], "little")
                if chunk_id == b"fmt ":
                    body = handle.read(size)
                    if len(body) < 16:
                        return None
                    code = int.from_bytes(body[0:2], "little")
                    channels = int.from_bytes(body[2:4], "little")
                    bits = int.from_bytes(body[14:16], "little")
                    if code == 0xFFFE and len(body) >= 26:
                        subtype = int.from_bytes(body[24:26], "little")
                        if subtype == 3:
                            code = 3                     # WAVE_FORMAT_IEEE_FLOAT
                    return code, channels, bits
                handle.seek(size + (size & 1), 1)
    except OSError:
        return None


def _read_float_wav(path: str) -> tuple[np.ndarray, int]:
    """Decode an IEEE-float WAV without relying on `wave` accepting it."""
    with open(path, "rb") as handle:
        header = handle.read(12)
        if (len(header) < 12 or header[:4] != b"RIFF"
                or header[8:12] != b"WAVE"):
            raise ValueError(f"{path}: not a RIFF/WAVE file")
        fmt = None
        raw = None
        while True:
            chunk = handle.read(8)
            if len(chunk) < 8:
                break
            chunk_id = chunk[:4]
            size = int.from_bytes(chunk[4:8], "little")
            if chunk_id == b"fmt ":
                fmt = handle.read(size)
                if size & 1:
                    handle.seek(1, 1)
            elif chunk_id == b"data":
                raw = handle.read(size)
                break
            else:
                handle.seek(size + (size & 1), 1)
    if fmt is None or raw is None or len(fmt) < 16:
        raise ValueError(f"{path}: malformed float WAV")
    channels = int.from_bytes(fmt[2:4], "little")
    sr = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    dtype = {32: "<f4", 64: "<f8"}.get(bits)
    if dtype is None or channels <= 0:
        raise ValueError(f"{path}: unsupported float WAV ({bits}-bit, {channels} channels)")
    samples = np.frombuffer(raw, dtype)
    frames = samples.size // channels
    samples = samples[:frames * channels].reshape(-1, channels).astype(np.float32)
    return samples, sr


def _validate_samples(x: np.ndarray, path: str) -> np.ndarray:
    """Refuse non-finite samples before any metric can turn them into NaN."""
    finite = np.isfinite(x)
    if not finite.all():
        count = int(np.count_nonzero(~finite))
        raise ValueError(f"{path}: {count} non-finite sample(s); refusing to measure corrupt data")
    return x


def load(path: str) -> tuple[np.ndarray, int]:
    """Return (samples[frames, channels] float32, sample_rate).

    Reads 8/16/24/32-bit integer WAV and 32/64-bit IEEE-float WAV. A Logic bounce
    is 24-bit by default, so assuming 16-bit silently produced garbage; float
    WAVs are likewise decoded by their real format code, not by sample width.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"audio file not found: {path}")

    if path.lower().endswith(".wav"):
        fmt = _wav_fmt(path)
        if fmt is not None and fmt[0] == 3:
            x, sr = _read_float_wav(path)
            channel_layout(x.shape[1])
            return _validate_samples(x, path), sr
        try:
            with wave.open(path) as w:
                sr, ch, n, width = (w.getframerate(), w.getnchannels(),
                                    w.getnframes(), w.getsampwidth())
                raw = w.readframes(n)
        except (wave.Error, EOFError):
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
        x = x.reshape(-1, ch)
        channel_layout(ch)
        return _validate_samples(x, path), sr

    return _load_via_soundfile(path)


def _load_via_soundfile(path: str) -> tuple[np.ndarray, int]:
    try:
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install soundfile to read this format (FLAC, OGG, AIFF)") from exc
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    channel_layout(data.shape[1])
    return _validate_samples(data, path), sr


def _welch_psd(data, sr: int, nperseg: int = 8192) -> tuple[np.ndarray, np.ndarray]:
    """Averaged periodogram (Welch, 50 % overlap, Hann), averaged over channels.

    One FFT over the whole file weights its centre and nearly ignores intro/outro.
    Welch averages every 8192-sample segment with 50 % overlap, so a loud section
    is not swallowed by the analysis window. Channels are averaged in the power
    domain: a mono fold-down would cancel out-of-phase content (and read a
    different spectrum from either channel).
    """
    x = np.asarray(data, dtype=np.float64)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    n = x.shape[0]
    if n < 2:
        return np.zeros(1), np.zeros(1)
    seg = min(nperseg, n)
    window = np.hanning(seg)
    step = max(1, seg // 2)
    starts = range(0, n - seg + 1, step) if n >= seg else [0]
    acc = np.zeros(seg // 2 + 1)
    count = 0
    for channel in range(x.shape[1]):
        column = x[:, channel]
        for start in starts:
            chunk = column[start:start + seg]
            if len(chunk) < seg:
                chunk = np.pad(chunk, (0, seg - len(chunk)))
            acc += np.abs(np.fft.rfft(chunk * window)) ** 2
            count += 1
    return np.fft.rfftfreq(seg, 1 / sr), acc / max(1, count)


def bands_db_rel(data, sr: int,
                 bands: dict[str, tuple[float, float]] | None = None) -> dict[str, float]:
    bands = BANDS if bands is None else bands
    freqs, psd = _welch_psd(data, sr)
    total = float(psd.sum()) + 1e-12
    return {name: round(10 * np.log10(float(psd[(freqs >= lo) & (freqs < hi)].sum()) / total + 1e-12), 1)
            for name, (lo, hi) in bands.items()}


def sibilance_db_rel(data, sr: int) -> float:
    """5-9 kHz energy relative to full-band energy (the band a de-esser acts on)."""
    return bands_db_rel(data, sr, {"sibilance": SIBILANCE_BAND})["sibilance"]


def _as_channels(data) -> np.ndarray:
    array = np.asarray(data, dtype=np.float64)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    if array.ndim != 2:
        raise ValueError("expected mono or multichannel samples")
    return array


def _k_weighted_channels(data, sr: int, chunk: int = 1 << 20) -> np.ndarray:
    """K-weight every channel once, in bounded-memory chunks.

    `integrated_lufs` and `loudness_range` need the same filtered signal; doing
    it twice doubled the dominant cost of a full-track measurement.

    Filtering each chunk on its own used to treat it as a complete periodic
    signal, so the filter's tail wrapped around at every chunk boundary. Each
    chunk now carries half a second of real pre-roll and zero padding on both
    sides: the padding absorbs the circular wrap, and the pre-roll lets the IIR
    state settle before the samples that are kept. The result is independent of
    the chunk size (the 38 Hz high-pass has a ~4 ms time constant, so 0.5 s is
    far longer than its decay).
    """
    x = _as_channels(data)
    n = x.shape[0]
    out = np.empty_like(x)
    pad = max(2048, min(int(0.5 * sr), n))
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        left = min(pad, start)
        right = min(pad, n - stop)
        base, end = start - left, stop + right
        for channel in range(x.shape[1]):
            segment = x[base:end, channel]
            padded = np.concatenate([np.zeros(pad), segment, np.zeros(pad)])
            filtered = _k_weighted(padded, sr)
            out[start:stop, channel] = filtered[pad + left:pad + left + (stop - start)]
    return out


def integrated_lufs(data, sr: int, weighted=None) -> float:
    """BS.1770 integrated loudness, summed across channels by their weights.

    Channel powers add (L^2 + R^2), they do not average: averaging a stereo signal
    under-reports by ~3 LU, and a hard out-of-phase pair reads silence. 5.1 uses
    the standard surround weights and excludes the LFE channel.
    """
    x = _as_channels(data)
    if weighted is None:
        weighted = _k_weighted_channels(x, sr)
    else:
        weighted = _as_channels(weighted)
    weights = _channel_weights(x.shape[1])

    n = x.shape[0]
    block = int(0.4 * sr)
    step = max(1, block // 4)
    if n < block:
        powers = [float(weights[c] * np.mean(weighted[:, c] ** 2))
                  for c in range(x.shape[1])]
        return round(-0.691 + 10 * np.log10(sum(powers) + 1e-20), 2)

    starts = range(0, n - block + 1, step)
    powers = np.array([sum(float(weights[c] * np.mean(weighted[i:i + block, c] ** 2))
                           for c in range(x.shape[1])) for i in starts]) + 1e-20
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
    x = _as_channels(data)
    phases = _interp_phases(oversample)
    peak = max(_true_peak_channel(x[:, c], phases) for c in range(x.shape[1]))
    return round(20 * np.log10(peak + 1e-12), 2)


def loudness_range(data, sr: int, win_s: float = 3.0, hop_s: float = 0.1,
                   weighted=None) -> float | None:
    """EBU R128 loudness range (LU), per EBU Tech 3342.

    K-weighted 3 s short-term loudness every 100 ms, absolute gate at -70 LUFS,
    relative gate 20 LU below the gated mean, then P95 - P10 of what survives.
    5.1 channel weights are the same as for integrated loudness (LFE excluded).

    The previous implementation used unweighted RMS windows with no gating and
    its own percentiles: a metric that does not mean what `loudness_range_lu`
    means to every other tool. Returns None when the file is shorter than one
    window (the range is not measurable, not zero).
    """
    x = _as_channels(data)
    n = x.shape[0]
    win = int(round(win_s * sr))
    if n < win:
        return None
    hop = max(1, int(round(hop_s * sr)))
    if weighted is None:
        weighted = _k_weighted_channels(x, sr)
    else:
        weighted = _as_channels(weighted)
    weights = _channel_weights(x.shape[1])
    power = np.zeros(n)
    for c in range(x.shape[1]):
        power += weights[c] * weighted[:, c] ** 2
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


def _front_stereo(data: np.ndarray) -> np.ndarray:
    """The L/R pair. In 5.1 the other channels are not averaged into a fold-down."""
    return data[:, :2] if data.ndim == 2 and data.shape[1] >= 2 else data


def stereo_correlation(stereo: np.ndarray) -> float:
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return 1.0
    l, r = stereo[:, 0], stereo[:, 1]
    denom = np.std(l) * np.std(r)
    if denom < 1e-9:
        return 1.0
    return round(float(np.corrcoef(l, r)[0, 1]), 3)


def mono_cancel_db(stereo: np.ndarray) -> float:
    """Mono fold-down level change in dB: 0 is mono-safe, negative is loss."""
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return 0.0
    mid = stereo.mean(axis=1)
    worst = max(float(np.sqrt(np.mean(stereo[:, c] ** 2))) for c in range(stereo.shape[1]))
    if worst < 1e-9:
        return 0.0
    return round(float(20 * np.log10(float(np.sqrt(np.mean(mid ** 2))) / worst + 1e-12)), 1)


def _flat_top_frames(data: np.ndarray, min_run: int = 3, tolerance: float = 1e-7,
                     floor: float = 0.5) -> int:
    """Frames inside a run of >= `min_run` identical loud samples.

    A limiter that clips at -1 dBFS never reaches the 0.999 threshold, but the
    repeated sample values are still a hard ceiling and audible distortion.
    Runs are found per channel and unioned at frame level.
    """
    x = np.atleast_2d(data) if data.ndim > 1 else data.reshape(-1, 1)
    if x.shape[0] < min_run:
        return 0
    flagged = np.zeros(x.shape[0], dtype=bool)
    for channel in range(x.shape[1]):
        column = x[:, channel]
        if column.size < min_run:
            continue
        same = np.abs(np.diff(column)) <= tolerance
        starts = np.concatenate(([True], ~same))
        run_id = np.cumsum(starts) - 1
        run_len = np.bincount(run_id)
        qualifies = run_len >= min_run
        qualifies &= np.abs(column[starts]) >= floor
        flagged |= qualifies[run_id]
    return int(flagged.sum())


def clipping(stereo: np.ndarray, thr: float = 0.999) -> dict:
    """Clipped FRAMES, not channel samples.

    A frame counts once even when both channels clip, so the proportion gate's
    denominator is duration * sample_rate, independent of channel count.
    `flat_top_frames` covers a ceiling below full scale (runs of equal samples).
    """
    hits = np.abs(stereo) >= thr
    count = int(hits.any(axis=1).sum()) if hits.ndim > 1 else int(hits.sum())
    return {"count": count, "flat_top_frames": _flat_top_frames(np.asarray(stereo))}


def noise_floor_dbfs(data, sr: int, win_s: float = 0.1):
    """Noise floor measured **only in silence**. Returns None when there is no silence.

    On continuous music the quietest windows are soft passages, not noise: reporting them
    as a floor would flag every track. Without a gap (or stems) the floor is unmeasurable.
    The window RMS sums channel power (sqrt(mean over every sample)), not a fold-down.
    """
    x = _as_channels(data)
    win = max(1, int(win_s * sr))
    n = x.shape[0]
    if n < win * 4:
        return None
    rms = np.array([float(np.sqrt(np.mean(x[i:i + win] ** 2)))
                    for i in range(0, n - win, win)])
    if rms.size == 0:
        return None
    db = 20 * np.log10(rms + 1e-12)
    quietest, median = float(np.percentile(db, 2)), float(np.percentile(db, 50))
    if median - quietest < 20.0:   # no real gap: the quiet windows are just soft music
        return None
    in_silence = db[db <= quietest + 6.0]
    return round(float(np.percentile(in_silence, 90)), 1) if in_silence.size else None


def _band_db(freqs: np.ndarray, psd: np.ndarray, lo: float, hi: float,
             total: float) -> float | None:
    window = (freqs >= lo) & (freqs < hi)
    if not window.any():
        return None
    return 10 * np.log10(float(psd[window].sum()) / total + 1e-12)


def _cutoff_from_psd(freqs: np.ndarray, psd: np.ndarray, sr: int) -> dict:
    """Locate the largest level cliff above 10 kHz, using the whole-file PSD.

    The old version only compared 14-16 kHz with 16-18 kHz and always reported
    `cutoff_hz: 16000`, so a 19 kHz cut and a full-band file looked the same.
    This is an estimate from adjacent 1 kHz bands, not a codec detector.
    """
    nyquist = sr / 2
    total = float(psd.sum()) + 1e-12
    if nyquist <= 10000:
        return {"cutoff_hz": round(nyquist), "drop_db": 0.0, "lossy_suspected": False,
                "note": "sample rate too low to test for a lossy cutoff"}
    edges = []
    for edge in range(10000, int(nyquist), 1000):
        below = _band_db(freqs, psd, edge - 1000, edge, total)
        above = _band_db(freqs, psd, edge, min(edge + 1000, nyquist), total)
        if below is None or above is None:
            continue
        edges.append((edge, float(below - above)))
    # A hard cliff usually spans two adjacent 1 kHz bands: the first band after
    # the transition still holds window leakage, the next one is silent, so the
    # largest single drop lands one band *above* the real cutoff. Group adjacent
    # significant edges into one cliff and report its lower frequency instead.
    clusters, current = [], []
    for edge, drop in edges:
        if drop > 20.0 and (not current or edge - current[-1][0] <= 1000):
            current.append((edge, drop))
        elif drop > 20.0:
            clusters.append(current)
            current = [(edge, drop)]
        elif current:
            clusters.append(current)
            current = []
    if current:
        clusters.append(current)
    if not clusters:
        best_drop = max((d for _, d in edges), default=0.0)
        return {"cutoff_hz": None, "drop_db": round(best_drop, 1),
                "lossy_suspected": False}
    cliff = max(clusters, key=lambda c: max(d for _, d in c))
    cutoff = cliff[0][0]
    return {"cutoff_hz": int(cutoff), "drop_db": round(max(d for _, d in cliff), 1),
            "lossy_suspected": bool(cutoff < 0.95 * nyquist)}


def _spectral_stats(data, sr: int) -> dict:
    """Bands, sibilance, centroid and cutoff from one PSD pass."""
    freqs, psd = _welch_psd(data, sr)
    total = float(psd.sum()) + 1e-12
    bands = {name: round(10 * np.log10(float(psd[(freqs >= lo) & (freqs < hi)].sum())
                                       / total + 1e-12), 1)
             for name, (lo, hi) in BANDS.items()}
    sibilance = round(_band_db(freqs, psd, *SIBILANCE_BAND, total), 1)
    centroid = int(float((freqs * psd).sum() / (psd.sum() + 1e-12)))
    return {"bands_db_rel": bands, "sibilance_db_rel": sibilance,
            "centroid_hz": centroid, "bandwidth": _cutoff_from_psd(freqs, psd, sr)}


def spectral_cutoff(data, sr: int) -> dict:
    freqs, psd = _welch_psd(data, sr)
    return _cutoff_from_psd(freqs, psd, sr)


def analyze_master(path: str) -> dict:
    data, sr = load(path)
    channels = int(data.shape[1])
    weighted = _k_weighted_channels(data, sr)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    # Crest uses the same population on both sides: all-channel peak / all-channel
    # RMS. The old mono-RMS denominator reported ~220 dB for an out-of-phase file.
    rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)) + 1e-12)
    spectral = _spectral_stats(data, sr)
    stereo = _front_stereo(data)
    stats = {
        "file": os.path.abspath(path),
        "sample_rate": sr,
        "channels": channels,
        "channel_layout": channel_layout(channels),
        "duration_seconds": round(data.shape[0] / sr, 2),
        "bit_depth": _wav_bit_depth(path),
        "loudness": {
            "integrated_lufs": integrated_lufs(data, sr, weighted=weighted),
            "loudness_range_lu": loudness_range(data, sr, weighted=weighted),
            "true_peak_dbtp": true_peak_dbtp(data, sr),
            "sample_peak_dbfs": round(20 * np.log10(peak + 1e-12), 2),
            "crest_db": round(20 * np.log10(peak / rms + 1e-12), 2),
        },
        "spectral": {
            "bands_db_rel": spectral["bands_db_rel"],
            "sibilance_db_rel": spectral["sibilance_db_rel"],
            "centroid_hz": spectral["centroid_hz"],
        },
        "stereo": {"phase_correlation": stereo_correlation(stereo),
                   "mono_cancel_db": mono_cancel_db(stereo),
                   "band_correlation": band_correlation(stereo, sr)},
        "clipping": clipping(data),
        "noise": {"floor_dbfs": noise_floor_dbfs(data, sr)},
        "bandwidth": spectral["bandwidth"],
    }
    return plain(stats)


def _wav_bit_depth(path: str) -> int | None:
    if not path.lower().endswith(".wav"):
        return None
    fmt = _wav_fmt(path)
    if fmt is not None and fmt[2] > 0:
        return int(fmt[2])
    try:
        with wave.open(path) as w:
            return w.getsampwidth() * 8
    except (wave.Error, EOFError):
        return None


def _centroid(data, sr: int) -> int:
    freqs, psd = _welch_psd(data, sr)
    return int(float((freqs * psd).sum() / (psd.sum() + 1e-12)))


def _band_correlation(l: np.ndarray, r: np.ndarray, sr: int,
                      lo: float, hi: float) -> float | None:
    """L/R correlation inside one band, or None when that band is nearly empty.

    A global correlation is dominated by the loudest band; a mono bass under wide
    highs can read healthy. The energy guard is relative (-30 dB), not absolute.
    """
    spec_l, spec_r = np.fft.rfft(l), np.fft.rfft(r)
    freqs = np.fft.rfftfreq(len(l), 1 / sr)
    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():
        return None
    low_power = float(np.sum(np.abs(spec_l[mask]) ** 2) + np.sum(np.abs(spec_r[mask]) ** 2))
    total_power = float(np.sum(np.abs(spec_l) ** 2) + np.sum(np.abs(spec_r) ** 2))
    if total_power <= 1e-20 or low_power < total_power * 1e-3:
        return None
    band_l = np.fft.irfft(spec_l * mask, n=len(l))
    band_r = np.fft.irfft(spec_r * mask, n=len(r))
    denom = np.std(band_l) * np.std(band_r)
    if denom < 1e-9:
        return None
    return round(float(np.corrcoef(band_l, band_r)[0, 1]), 3)


def low_band_correlation(stereo: np.ndarray, sr: int, cutoff_hz: float = 120.0):
    """Mono-bass check: L/R correlation below the cutoff, where a stereo bass collapses.

    `None` means the low band carries too little energy to judge, not "healthy".
    """
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return None
    return _band_correlation(stereo[:, 0], stereo[:, 1], sr, 0.0, cutoff_hz)


def band_correlation(stereo: np.ndarray, sr: int) -> dict:
    """Per-band L/R correlation; informative, and `None` per empty band."""
    if stereo.ndim != 2 or stereo.shape[1] < 2:
        return {band: None for band in BANDS}
    return {band: _band_correlation(stereo[:, 0], stereo[:, 1], sr, lo, hi)
            for band, (lo, hi) in BANDS.items()}


def analyze_reference(path: str) -> dict:
    """Measure a commercial reference: its band balance becomes the reference curve."""
    data, sr = load(path)
    channels = int(data.shape[1])
    weighted = _k_weighted_channels(data, sr)
    spectral = _spectral_stats(data, sr)
    return plain({
        "file": os.path.abspath(path),
        "channel_layout": channel_layout(channels),
        "reference_curve": spectral["bands_db_rel"],
        "sibilance_db_rel": spectral["sibilance_db_rel"],
        # Loudness and peaks are summed across channels, not read from the mono
        # fold-down: that under-reported a correlated stereo reference by ~3 LU.
        "integrated_lufs": integrated_lufs(data, sr, weighted=weighted),
        "true_peak_dbtp": true_peak_dbtp(data, sr),
        "loudness_range_lu": loudness_range(data, sr, weighted=weighted),
        "phase_correlation": stereo_correlation(_front_stereo(data)),
    })


def analyze_sections(path: str, window_s: float = 30.0) -> dict:
    """Per-window analysis. A hot chorus disappears in a whole-file average.

    `flagged` is informational: a chorus may legitimately differ from the median.
    The final window may be partial (>= 5 s); a shorter remainder is reported as
    `tail_dropped_s` instead of vanishing.
    """
    data, sr = load(path)
    win = int(window_s * sr)
    total = data.shape[0]
    if total < win:
        return {"window_s": window_s, "windows": [], "reliable": False,
                "analyzed_seconds": round(total / sr, 2),
                "total_seconds": round(total / sr, 2),
                "note": "file shorter than one window; use analyze_master"}
    windows = []
    starts = list(range(0, total - win + 1, win))
    remainder = total - starts[-1] - win
    if remainder >= int(5.0 * sr):
        starts.append(starts[-1] + win)
    for start in starts:
        stop = min(start + win, total)
        chunk = data[start:stop]
        weighted = _k_weighted_channels(chunk, sr)
        window = {"start_s": round(start / sr, 1),
                  "duration_s": round((stop - start) / sr, 2),
                  # measure the stereo window: a mono fold-down reads ~3 LU low
                  "integrated_lufs": integrated_lufs(chunk, sr, weighted=weighted),
                  "bands_db_rel": bands_db_rel(chunk, sr),
                  "phase_correlation": stereo_correlation(_front_stereo(chunk))}
        if stop - start < win:
            window["partial"] = True
        windows.append(window)
    median_lufs = float(np.median([w["integrated_lufs"] for w in windows]))
    median_bands = {b: float(np.median([w["bands_db_rel"][b] for w in windows])) for b in BANDS}
    tilt = [w["bands_db_rel"]["high_mid"] - w["bands_db_rel"]["bass"] for w in windows]
    median_tilt = float(np.median(tilt))
    for w, t in zip(windows, tilt):
        w["deviation_db"] = {b: round(w["bands_db_rel"][b] - median_bands[b], 1) for b in BANDS}
        w["lufs_delta"] = round(w["integrated_lufs"] - median_lufs, 1)
        w["tilt"] = round(t, 1)
        w["tilt_delta"] = round(t - median_tilt, 1)
        # Informational only: a single robust criterion, not a send-back gate.
        w["flagged"] = abs(w["lufs_delta"]) > 3.0 or abs(w["tilt_delta"]) > 3.0
    reliable = len(windows) >= 3
    if not reliable:                      # a median of two points is degenerate
        for w in windows:
            w["flagged"] = False
    analyzed = min(total, starts[-1] + win)
    return plain({"window_s": window_s, "windows": windows, "reliable": reliable,
                  "analyzed_seconds": round(analyzed / sr, 2),
                  "total_seconds": round(total / sr, 2),
                  "tail_dropped_s": round(max(0, total - analyzed) / sr, 2),
                  "median_lufs": round(median_lufs, 2),
                  "median_bands_db_rel": {k: round(v, 1) for k, v in median_bands.items()},
                  "median_tilt": round(median_tilt, 1),
                  "flagged_windows": [w["start_s"] for w in windows if w["flagged"]],
                  "note": "flag deviations are informational; a chorus can differ "
                          "from the median without being a problem"})


def _window_band_powers(data: np.ndarray, sr: int, window_s: float = 1.0,
                        hop_s: float = 0.5) -> list[dict] | None:
    """Per-window RMS and relative band energies, for temporal co-activity."""
    win = int(window_s * sr)
    hop = max(1, int(hop_s * sr))
    if win <= 0 or data.shape[0] < win:
        return None
    rows = []
    for start in range(0, data.shape[0] - win + 1, hop):
        chunk = data[start:start + win]
        rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
        freqs, psd = _welch_psd(chunk, sr)
        total = float(psd.sum()) + 1e-12
        bands = {name: float(10 * np.log10(
            float(psd[(freqs >= lo) & (freqs < hi)].sum()) / total + 1e-12))
            for name, (lo, hi) in BANDS.items()}
        rows.append({"rms": rms, "bands": bands})
    return rows


def _temporal_overlap(a: dict, b: dict) -> dict:
    """Overlap measured only while both stems are active (RMS > stem max - 40 dB).

    The static band minimum over whole files claims two stems collide even when
    they never sound together. `band_overlap_db` is time-weighted by the fraction
    of windows in which both are active, so a rare collision does not score like
    a constant one.
    """
    rows_a, rows_b = a.get("windows"), b.get("windows")
    if not rows_a or not rows_b or a.get("sr") != b.get("sr"):
        return {"band_overlap_db": None, "dominant_band": None,
                "dominant_band_overlap_db": None, "coactive_fraction": 0.0,
                "note": "sample rates differ or files too short for 1 s windows"}
    count = min(len(rows_a), len(rows_b))
    if count == 0:
        return {"band_overlap_db": None, "dominant_band": None,
                "dominant_band_overlap_db": None, "coactive_fraction": 0.0,
                "note": "no analysis windows"}
    peak_a = max(row["rms"] for row in rows_a) or 1e-12
    peak_b = max(row["rms"] for row in rows_b) or 1e-12
    floor_a, floor_b = peak_a * 1e-2, peak_b * 1e-2       # -40 dB
    coactive = [index for index in range(count)
                if rows_a[index]["rms"] >= floor_a and rows_b[index]["rms"] >= floor_b]
    fraction = len(coactive) / count
    if not coactive:
        return {"band_overlap_db": None, "dominant_band": None,
                "dominant_band_overlap_db": None,
                "coactive_fraction": round(fraction, 3),
                "note": "the stems never sound together in the same 1 s window"}
    shared = {}
    for band in BANDS:
        powers = [10 ** (min(rows_a[i]["bands"][band], rows_b[i]["bands"][band]) / 10)
                  for i in coactive]
        # Time-weighted: the same collision present 5 % of the time is 13 dB rarer.
        shared[band] = 10 * np.log10(float(np.mean(powers)) * fraction + 1e-12)
    overlap = float(sum(shared.values()) / len(shared))
    dominant = max(shared, key=shared.get)
    return {"band_overlap_db": round(overlap, 2), "dominant_band": dominant,
            "dominant_band_overlap_db": round(shared[dominant], 2),
            "coactive_fraction": round(fraction, 3)}


def analyze_stems(paths: list[str]) -> dict:
    """Per-stem measurements plus a roughness matrix between stems."""
    stems = []
    loaded = []
    for p in paths:
        data, sr = load(p)
        channels = int(data.shape[1])
        weighted = _k_weighted_channels(data, sr)
        spectral = _spectral_stats(data, sr)
        stems.append({
            "name": os.path.splitext(os.path.basename(p))[0],
            "path": os.path.abspath(p),
            "channel_layout": channel_layout(channels),
            "rms_dbfs": round(20 * np.log10(np.sqrt(np.mean(
                data.astype(np.float64) ** 2)) + 1e-12), 2),
            "integrated_lufs": integrated_lufs(data, sr, weighted=weighted),
            "phase_correlation": stereo_correlation(_front_stereo(data)),
            "low_instrument": bool(spectral["bands_db_rel"]["bass"] > -12.0),
            "peak_dbfs": round(20 * np.log10(np.max(np.abs(data)) + 1e-12), 2),
            "bands_db_rel": spectral["bands_db_rel"],
            "sibilance_db_rel": spectral["sibilance_db_rel"],
            "low_correlation": low_band_correlation(_front_stereo(data), sr),
        })
        loaded.append({"sr": sr, "windows": _window_band_powers(data, sr)})
    overlap_pairs = []
    for i in range(len(stems)):
        for j in range(i + 1, len(stems)):
            a, b = stems[i], stems[j]
            measured = _temporal_overlap(loaded[i], loaded[j])
            overlap_pairs.append({"a": a["name"], "b": b["name"], **measured})
    # A pair with unmeasured overlap sorts last; measured pairs by the shared energy.
    overlap_pairs.sort(key=lambda m: -(-999.0 if m["band_overlap_db"] is None
                                       else m["band_overlap_db"]))
    return plain({"stems": stems,
                  "band_overlap_pairs": overlap_pairs[:5],
                  "band_overlap_note": "time-weighted mean per-band minimum while both "
                                       "stems are active (> max -40 dB); a crowding proxy, "
                                       "not perceptual masking",
                  "gates": stem_gates(stems)})


VOCAL_HINTS = ("vocal", "vox", "voice", "voz")


def _stem_lufs(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if np.isfinite(number) else None


def stem_gates(stems: list[dict]) -> list[dict]:
    """Conditions that need a mix fix, measured on the exported stems."""
    out: list[dict] = []
    vocals = [s for s in stems if any(h in s["name"].lower() for h in VOCAL_HINTS)]
    rest = [s for s in stems if s not in vocals]
    vocal_lufs = [lufs for lufs in (_stem_lufs(s.get("integrated_lufs")) for s in vocals)
                  if lufs is not None]
    other_lufs = [lufs for lufs in (_stem_lufs(s.get("integrated_lufs")) for s in rest)
                  if lufs is not None]
    if vocal_lufs and other_lufs:
        # Every vocal stem contributes; the lead is not necessarily the only one.
        v_power = sum(10 ** (lufs / 10) for lufs in vocal_lufs)
        others = sum(10 ** (lufs / 10) for lufs in other_lufs)
        relative = round(float(10 * np.log10(v_power / (others + 1e-20) + 1e-20)), 1)
        if relative < -12.0:
            out.append({"gate": "vocal_buried", "stem": vocals[0]["name"],
                        "relative_db": relative})

    measured_lufs = [lufs for lufs in (_stem_lufs(s.get("integrated_lufs")) for s in stems)
                     if lufs is not None]
    if len(measured_lufs) >= 3:
        # Sustained loudness, not a transient peak: a snare spike is not dominance.
        excess = round(float(max(measured_lufs) - np.median(measured_lufs)), 1)
        if excess > 6.0:
            loudest = max((s for s in stems
                           if _stem_lufs(s.get("integrated_lufs")) is not None),
                          key=lambda s: _stem_lufs(s.get("integrated_lufs")))
            out.append({"gate": "single_element_peak", "stem": loudest["name"],
                        "excess_db": excess})

    for stem in stems:
        correlation = stem.get("low_correlation")
        if (stem.get("low_instrument") and correlation is not None
                and float(correlation) < 0.3):
            out.append({"gate": "mono_bass", "stem": stem["name"],
                        "low_correlation": correlation})
    return out
