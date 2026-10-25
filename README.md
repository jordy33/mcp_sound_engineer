# mcp_sound_engineer

An MCP server that **measures audio and decides like a mix/master engineer**, returning a
DAW-neutral action plan.

It does not control any DAW. It is one of two independent MCPs:

```
┌───────────────────────┐   facts + answers + recipe   ┌──────────────────────┐
│  mcp_sound_engineer   │ ───────────────────────────► │  adapter MCP (yours) │
│  measure → judge      │        (neutral JSON)        │  applies the plan    │
└───────────────────────┘                              └──────────────────────┘
        numpy + Jev
```

Two hats: **mixing** (N stems: balance, masking, corrective EQ) and **mastering** (one stereo file:
send-back gates, dynamics, limiter, loudness target, dither).

---

## 1. Requirements

| What | Why |
|---|---|
| macOS or Linux, Python **3.9+** | Runs the server; the venv script assumes a POSIX shell |
| `python3 -m venv` available | The start script creates a local `.venv`; nothing is installed globally |
| Network access | Only for Jev decisions; measurement is fully offline |
| A Jev API key | See §2; `analyze_*` work without it |
| ~20 MB disk | numpy plus the venv |

No ffmpeg, no libsndfile, no system packages. WAV is read with the standard library; FLAC/OGG/AIFF
need the optional `soundfile` extra.

## 2. Getting a Jev key

Jev is a *System One* decision model by TypeSafe AI. It answers `choice` / `score` / `noul` with
calibrated probabilities instead of prose — that is why the output is machine-actionable.

1. Open <https://jevtypesafeai.com/pricing#get-key> and create an account.
2. Create a key. Two kinds exist and they hit different endpoints:

   | Key looks like | Endpoint | Notes |
   |---|---|---|
   | `jv_live_…` | `https://jevtypesafeai.com/api/v1/decide` | Hosted playground/reseller |
   | `apikey_…` | `https://api.typesafe.ai/v1/systemone` | Official TypeSafe API (default here) |

3. Put it in `.env`:

```bash
JEV_API_KEY=apikey_...
JEV_ENDPOINT=https://api.typesafe.ai/v1/systemone   # optional
JEV_MODEL=jev-latest                                # optional
```

`.env` is git-ignored. The server re-reads it on every decision, so you can rotate the key without
restarting.

> A key from one endpoint does not work on the other: a `jv_live_` key against
> `api.typesafe.ai` returns `401`, and vice versa. If `judge_*` fails with an auth error, check which
> service issued the key. Without a key the server still starts, `analyze_*` works, and every
> `judge_*` call returns a clear error.

## 3. Install and run

```bash
cd mcp_sound_engineer
./start-sound-engineer-mcp.sh          # creates .venv, installs deps, starts HTTP on :8766
```

| Command | What it does |
|---|---|
| `./start-sound-engineer-mcp.sh` | HTTP bridge on `127.0.0.1:8766` (`POST /mcp`, `GET /health`) |
| `./start-sound-engineer-mcp.sh --port 8770` | Same, another port |
| `./start-sound-engineer-mcp.sh --stdio` | JSON-RPC over stdin/stdout (MCP clients that spawn it) |
| `./start-sound-engineer-mcp.sh --status` / `--stop` / `--restart` | Lifecycle via `.run/engineer.pid` |
| `./start-sound-engineer-mcp.sh --test` | Offline smoke test; does not need `.env` |

Register it in any MCP client, next to your DAW adapter — they are separate servers:

```json
{"mcpServers": {
  "engineer": {"command": "/abs/path/mcp_sound_engineer/start-sound-engineer-mcp.sh", "args": ["--stdio"]}
}}
```

## 4. Tools

| Tool | Input | Returns |
|---|---|---|
| `analyze_master` | one stereo file | LUFS, true peak, LRA, crest, phase correlation, 6 bands, centroid, clipping |
| `analyze_stems` | N files | per-stem loudness + bands, pairwise masking matrix |
| `judge_master` | path + platform | `answers` (typed) + `recipe` |
| `judge_mix` | stem paths | `answers` + `recipe` |
| `engineer_info` | — | rules version, Jev status, platforms |

## 5. The rules, and where they live

Sources: the [mastering-engineer methodology](https://github.com/fadelabs/phantom/blob/main/plugin/skills/mastering-engineer/SKILL.md)
(send-back gates, ten-stage chain, parameter ranges) and the
[2026 platform loudness targets](https://www.forasoft.com/learn/downloads/lufs-targets-per-platform-cheatsheet).

Everything numeric lives in [`server/rules.py`](server/rules.py); the wording lives in the question
schemas in [`server/engineer_server.py`](server/engineer_server.py).

| Rule | Value | In code | Wired? |
|---|---|---|---|
| Corrective EQ per point | ≤ 3 dB | `GATE.max_corrective_db_per_point` | ✅ clamps `eq.bell` |
| Cumulative corrective EQ | ≤ 6 dB | `GATE.max_cumulative_corrective_db` | ✅ running total in `build_master_recipe` |
| Phase correlation | < +0.3 → send back | `GATE.min_phase_correlation` | ✅ measured guard |
| Clipping that blocks mastering | ≥ 500 samples or ≥ 0.01 % | `MIN_CLIPPED_SAMPLES`, `MIN_CLIPPED_PROPORTION` | ✅ measured guard |
| Already a finished master | LRA < 4 LU **and** TP > −1 **and** LUFS > −12 | `MAX_LRA_FINISHED` | ✅ measured guard |
| Model gate threshold | 0.8 | `GATE_THRESHOLD` | ✅ |
| Broadband compression | 1.5–3:1, 10–30 ms, ≤ 3 dB GR | `CHAIN`, `GATE.max_gain_reduction_db` | ⚠️ recipe emits ratio/attack; GR comes from `dynamics_amount` |
| Limiter | ceiling −1.0 dBTP, ≤ 4 dB GR | `PLATFORMS`, `GATE.max_limiter_gr_db` | ✅ |
| Dither last | always | `CHAIN` | ✅ last action |
| Loudness targets | Spotify −14, Apple −16, … | `PLATFORMS` | ✅ |
| Noise floor | > −50 dB → send back | `GATE.noise_floor_*` | ❌ no detector yet |
| Lossy source | cliff > 20 dB above HF shelf | `GATE.lossy_shelf_drop_db` | ❌ no detector yet |
| Ten-stage chain order | HPF → … → dither | `CHAIN` | ⚠️ documented, not enforced as a pipeline |
| `eq.shelf`, `dynamic_eq`, `stereo.width`, `pan` | vocabulary only | `ACTIONS` | ❌ never emitted |

The ❌ rows are honest gaps: the constant exists, the detector does not. They are listed in
[`docs/LIMITS.md`](docs/LIMITS.md) and in §8.

### Adding a rule

Three edits, no config files:

1. **The number** → `server/rules.py`, in `GATE` (a cap) or `PLATFORMS`/`REFERENCE_CURVE` (a target).
2. **The judgement** → the question dict in `server/engineer_server.py`
   (`MASTER_QUESTIONS` or `MIX_QUESTIONS`). Use `noul` for gates, `score` 2–10 levels for magnitudes,
   `choice` for treatments — and write `instructions` that reference the measured fields, so the
   model judges the numbers rather than guessing from genre.
3. **The action** → `build_master_recipe` / `build_mix_recipe`, emitting a `_action(...)` from
   `ACTIONS`. Clamp it with the cap you added in step 1; never trust the model to respect it.

Then `./start-sound-engineer-mcp.sh --test` and a real call:

```bash
curl -s -X POST http://127.0.0.1:8766/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"judge_master","arguments":{"path":"/abs/track.wav"}}}'
```

## 6. The recipe contract

Recipes are ordered arrays of `action` primitives; no plugin, vendor or DAW name appears.
Full schema: [`docs/RECIPE.md`](docs/RECIPE.md) (currently `1.1.0`).

```json
[
  {"action": "eq.high_pass", "target": "master", "schema": "1.1.0", "freq_hz": 25, "slope_db_oct": 12},
  {"action": "eq.bell", "target": "master", "schema": "1.1.0", "freq_hz": 300, "gain_db": -2.0, "q": 5.0, "role": "corrective"},
  {"action": "limiter", "target": "master", "schema": "1.1.0", "ceiling_dbtp": -1.0, "target_lufs": -14.0},
  {"action": "send_back", "target": "master", "schema": "1.1.0", "reasons": ["already_mastered:measured"], "probabilities": {"already_mastered": 0.77}}
]
```

## 7. What has and has not been verified

Measured on 24 real music clips (8 tracks × 3 versions: original, +3.4 dB at 60–250 Hz, +5.3 dB at
6–16 kHz):

| Check | Result |
|---|---|
| Direction on +5.3 dB of added highs | 8/8 |
| Direction on +3.4 dB of added lows | 5/8 (both failures were scale saturation) |
| Spearman(measured tilt, "bright") | +0.77 |
| Stability across runs | ±1 point |
| End-to-end on a full 284 s track | 2.9 s per call |

**Not** verified: no human labels, one repetition per clip, synthetic perturbations. It measures
sensitivity to a known change, not agreement with a trained ear.

## 8. Status

`0.1.0`. What is missing is tracked in [`docs/LIMITS.md`](docs/LIMITS.md): noise-floor and lossy-source
detectors, reference-track analysis (the reference curve is still a constant), per-section analysis,
`dry_run`, confidence policy, packaging and CI.

## License

MIT.
