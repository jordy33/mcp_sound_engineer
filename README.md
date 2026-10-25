# mcp_sound_engineer

An MCP server that **measures audio and decides like a mix/master engineer** — and returns a
DAW-neutral action plan.

It does not control any DAW, and it never will. It is one of two separate MCPs:

```
┌───────────────────────┐   facts + answers + recipe   ┌──────────────────────┐
│  mcp_sound_engineer   │ ───────────────────────────► │  adapter MCP (any)   │
│  measure → judge      │        (neutral JSON)        │  adapter MCP (yours) │
└───────────────────────┘                              └──────────────────────┘
        numpy + Jev                                         applies the plan
```

Keeping them apart means the same judgement layer works with Logic, REAPER, Ableton, Pro Tools,
or a plain CLI — the adapter is replaceable, the engineer is not.

## Two hats, one server

| Role | Input | What it decides |
|---|---|---|
| **Mixing engineer** | N recorded stems | balance, masking, corrective EQ, bus glue, fader moves |
| **Mastering engineer** | one stereo file | send-back gates, corrective/tonal EQ, dynamics, limiter, loudness target, dither |

## Why a typed decision model

The judge is a *System One* decision model: it returns `choice` / `score` / `noul` with
probabilities instead of prose. That is an architectural requirement, not a preference:

* a `choice` can only return a label we declared — no invented categories;
* a `score` always lands on the scale we declared — no parsing, no regex;
* a `noul` is a number in [0,1] — thresholdable by code;
* ~0.3 s and ~$0.0002 per decision, so it can run inside an agent loop.

A generative model cannot be held to fixed rules: it may ignore the thresholds, invent a plugin, or
disagree with itself between two identical calls. For a decision that moves a fader, that is
disqualifying.

### What we verified

24 real music clips — 8 tracks x 3 versions (original, +3.4 dB at 60–250 Hz, +5.3 dB at 6–16 kHz):

| Check | Result |
|---|---|
| Direction on +5.3 dB of added highs | **8/8** |
| Direction on +3.4 dB of added lows | 5/8 (both failures were scale saturation, not blindness) |
| Spearman(measured tilt, "bright") | **+0.77** |
| Stability across runs | ±1 point |

### What we have not verified

No human labels yet. One repetition per clip. Perturbations are synthetic. This measures sensitivity
to a known change; it does **not** measure agreement with a trained ear. Every threshold in
`rules.py` is a starting point, not a validated constant.

## Tools

| Tool | Input | Returns |
|---|---|---|
| `analyze_master` | one stereo file | LUFS, true peak, LRA, crest, phase correlation, 6 bands, centroid, clipping |
| `analyze_stems` | N files | per-stem loudness + bands, pairwise masking matrix |
| `judge_master` | path + platform | `answers` (typed) + `recipe` (neutral actions) |
| `judge_mix` | stem paths | `answers` + `recipe` (faders, EQ, bus compression) |
| `engineer_info` | — | rules version, Jev config, platforms |

## The recipe contract

Recipes are arrays of actions. No plugin, vendor or DAW name ever appears:

```json
[
  {"action": "eq.high_pass", "target": "master", "schema": "1.0.0", "freq_hz": 25, "slope_db_oct": 12},
  {"action": "eq.bell",      "target": "master", "schema": "1.0.0", "freq_hz": 300, "gain_db": -2.0, "q": 5.0, "role": "corrective"},
  {"action": "de_esser",     "target": "master", "schema": "1.0.0", "freq_hz": 6500, "ratio": 2.0},
  {"action": "compressor",   "target": "mix_bus","schema": "1.0.0", "ratio": 2.0, "attack_ms": 20, "release_ms": 200, "gain_reduction_db": 0.5},
  {"action": "limiter",      "target": "master", "schema": "1.0.0", "ceiling_dbtp": -1.0, "expected_gain_reduction_db": 1.2, "target_lufs": -14.0},
  {"action": "dither",       "target": "master", "schema": "1.0.0", "bit_depth": 16}
]
```

Full schema: [`docs/RECIPE.md`](docs/RECIPE.md). An adapter maps `eq.bell` → Channel EQ band (Logic),
any EQ. It must reject anything outside the caps and report back what it
actually applied.

## The rules

Professional mastering methodology, enforced as hard caps
([mastering-engineer](https://github.com/fadelabs/phantom/blob/main/plugin/skills/mastering-engineer/SKILL.md),
[mastering guide 2026](https://www.masteringthemix.com/pages/audio-mastering-guide)).

**Gates — when to send the mix back instead of mastering it:**

| Condition | Threshold |
|---|---|
| Corrective EQ at a single point | > 3 dB |
| Cumulative corrective EQ | > 6 dB |
| Phase correlation | < +0.3 (elements vanish in mono) |
| Noise floor | > −50 dB |
| Lossy source | drop > 20 dB above the HF shelf |
| Baked-in clipping | any |

**The ten stages, in order:** high-pass 20–30 Hz (6–12 dB/oct) → corrective EQ (Q 4–8, ≤3 dB) →
denoise (tonal → clicks → broadband) → broadband compression (1.5–3:1, 10–30 ms attack, ≤3 dB GR) →
dynamic EQ → tonal EQ (Q 0.5–1.5) → stereo (mono <120 Hz) → limiter (−1.0 dBTP, ≤4 dB GR) →
loudness target → dither last.

**Loudness targets:** Spotify −14, Apple Music −16, YouTube −14, Amazon −14/−2 dBTP, Tidal −14,
CD −9/−0.1, podcast −16.

## Measurement notes

* `integrated_lufs` uses `pyloudnorm` when installed; otherwise a K-weighting + RMS approximation.
* `true_peak_dbtp` is 4× oversampled via FFT zero-padding (BS.1770 minimum), not 8×.
* `loudness_range_lu` is EBU-style short-term P95−P10, indicative not certified.
* `phase_correlation` is Pearson between L/R over the whole file — it does not localize problems.
* WAV via stdlib; FLAC/OGG/AIFF need `soundfile`.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add JEV_API_KEY
python tests/test_smoke.py
```

## Run

```bash
python server/engineer_server.py    # newline-delimited JSON-RPC 2.0 on stdin/stdout
```

Register it next to whichever adapter you use — they are independent servers:

```json
{
  "mcpServers": {
    "engineer": { "command": "python", "args": ["/abs/path/mcp_sound_engineer/server/engineer_server.py"] },
    "logic":    { "command": "mcp-logic", "args": [] }
  }
}
```

## Status

`0.1.0`. Measurement layer and rules are real and tested; Jev schemas are version 1.
See [`docs/LIMITS.md`](docs/LIMITS.md) for what this does **not** do, and the roadmap below.

### Roadmap (what's missing)

1. **Detectors that the gates already reference but are not implemented**: noise floor, lossy
   source, baked-in clipping beyond a sample counter, phase-vs-frequency.
2. **Reference analysis** — measure a commercial reference and compute real per-band deviation.
   This is ~70 % of mastering practice and today the reference curve is a constant.
3. **Per-section analysis** (verse/chorus) instead of whole-file numbers.
4. **Better masking metric** for stems (band-wise overlap weighted by level, not an average).
5. **Recipe application report** from adapters + a `dry_run` mode.
6. **Confidence policy**: refuse to act below a threshold instead of always answering.
7. Packaging (`pyproject.toml`), CI, versioned schema migrations, fixture audio.

## License

MIT.
