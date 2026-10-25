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
| `analyze_stems` | N files | per-stem loudness, bands, low-band correlation, pairwise masking, and stem gates (buried vocal, outlier element, mono bass) |
| `analyze_sections` | path + window | per-window LUFS/bands/correlation and which windows deviate from the median |
| `analyze_reference` | path | a commercial reference's band balance, to feed `judge_master` |
| `validate_recipe` | recipe | adapter-side contract check: caps, schema, terminal `send_back` |
| `judge_master` | path, platform, optional `reference_path` / `previous_path` | `answers` + `recipe` + `warnings`; **measured gates run before the model**, so a refusal needs no key |
| `judge_mix` | stem paths | `answers` + `recipe` |
| `engineer_info` | — | rules version, Jev status, platforms |

## 5. The rules, and where they live

Sources: the [mastering-engineer methodology](https://github.com/fadelabs/phantom/blob/main/plugin/skills/mastering-engineer/SKILL.md)
(send-back gates, ten-stage chain, parameter ranges) and a third-party
[2026 loudness cheat sheet](https://www.forasoft.com/learn/downloads/lufs-targets-per-platform-cheatsheet)
that has **not** been checked against primary platform docs.

Everything numeric lives in [`server/rules.py`](server/rules.py), together with the question schemas:
one source of truth for both the numbers and the answer keys.

| Rule | Value | In code | Behaviour |
|---|---|---|---|
| Phase correlation | < +0.3 | `GATE.min_phase_correlation` | **measured** → `send_back` |
| Clipping that blocks mastering | ≥ 500 frames **and** ≥ 0.01 % | `MIN_CLIPPED_*` | **measured** → `send_back` |
| Already a finished master | LRA < 4 **and** TP > −1 **and** LUFS > −12 | `MAX_LRA_FINISHED` | **measured** → `send_back` (product rule) |
| Corrective EQ per point | ≤ 3 dB | `GATE.max_corrective_db_per_point` | over the cap → `send_back`, never clamped |
| Cumulative corrective EQ | ≤ 6 dB | `GATE.max_cumulative_corrective_db` | includes the sibilance cut |
| Total gain reduction | ≤ 6 dB (source range 6–8) | `GATE.max_total_gain_reduction_db` | compressor + limiter summing over budget → `send_back` |
| Mono compatibility | fold-down loss < 6 dB | `GATE.min_mono_cancel_db` | warning with the measured loss |
| Gain staging (mix) | every stem toward −18 LUFS | `GAIN_STAGING_*` | deterministic faders, ±6 dB max |
| Phase before EQ (mix) | correlation < +0.3 | `phase_before_eq` | `polarity.invert` or `eq.high_pass_stem` |
| Confidence | < 0.5 | `CONFIDENCE_MIN` | `confidence.needs_human` + warning; the recipe is a draft |
| Broadband compression | 1.5–3:1, 10–30 ms, 1–3 dB GR | `CHAIN`, `GATE.min/max_gain_reduction_db` | emitted on the master bus |
| De-esser | after compression | `CHAIN` | emitted after the compressor |
| Limiter | ceiling from the destination, GR computed | `expected_limiter_gr` | arithmetic from measured LUFS, not a model score |
| Dither | only when the destination needs fewer bits than the source | `PLATFORMS[...]`, `facts.bit_depth` | none for streaming; TPDF for CD/podcast only if reducing |
| Playback normalization | Spotify −14, Apple −16, … | `PLATFORMS` | informational; never a delivery target |
| Unknown destination | — | `loudness_target` | raises, no silent fallback |
| Reference curve | measured from a real track, or the normalized default | `analyze_reference`, `REFERENCE_CURVE` | linear sum = 1.0 when no reference is passed |
| Correlation drop after the chain | > 0.15 | `correlation_drop` | warning when `previous_path` is given |
| Noise floor | > −50 dB | `GATE.noise_floor_moderate_db` | **measured in silence only**; `None` when the file has no gap |
| Lossy source | cliff > 20 dB between 14–16 k and 16–18 k | `GATE.lossy_shelf_drop_db` | **measured** → `send_back` |
| `eq.shelf`, `dynamic_eq`, `stereo.width`, `pan` | vocabulary only | `ACTIONS` | ❌ never emitted |

**The decision model never gates.** It scores and chooses treatments; every refusal is measured.
Asking it "should this be sent back?" produced a global yes/no that contradicted its own
component scores (0.81 refuse, yet `low_end` 1.46 and `high_mid` 1.68 — both "slight").

### How to know a rule is real

```bash
./start-sound-engineer-mcp.sh --test     # 82 assertions: smoke + rules + analysis + adapter
```

`tests/test_rules.py` asserts behaviour, not arithmetic:

* every balance label lowers the offender, and a fader points at a real exported stem;
* a corrective request above 3 dB returns `send_back` instead of a clamped band;
* 60 clipped frames in 10 s do **not** send back, 5000 do;
* `dither` never appears for a streaming destination;
* the two recipe builders reject any answer key missing from their schema (this is the test that
  catches a `low_end` / `low_mid` mismatch);
* an unknown destination or unknown action target raises instead of falling back.

`tests/test_analysis.py` uses signals whose correct value is known: a 997 Hz sine at −20 dBFS must
read −20 LUFS ±1, a 440 Hz sine must show 3.01 dB crest, inverted channels must correlate at −1.0,
and trailing silence must not change integrated loudness by more than 1 LU (BS.1770 gating).

When you change a rule, break it on purpose first. If the suite stays green, the test is decoration.

### Adding a rule

1. **The number** → `GATE` (a cap) or `PLATFORMS` / `REFERENCE_CURVE` (a target).
2. **The judgement** → `MASTER_QUESTIONS` / `MIX_QUESTIONS` in the same file. `noul` only for
   something that cannot be measured; `score` for magnitudes; `choice` for treatments.
3. **The action** → `build_master_recipe` / `build_mix_recipe`, emitting `_action(...)`. Enforce the
   cap in code; never trust the model to respect it.

## 6. The recipe contract

Recipes are ordered arrays of `action` primitives; no plugin, vendor or DAW name appears.
Full schema: [`docs/RECIPE.md`](docs/RECIPE.md) (currently `2.0.0`).

```json
[
  {"action": "eq.high_pass", "target": "master", "schema": "2.0.0", "freq_hz": 25, "slope_db_oct": 12},
  {"action": "eq.bell", "target": "master", "schema": "2.0.0", "freq_hz": 300, "gain_db": -2.0, "q": 5.0, "role": "corrective"},
  {"action": "limiter", "target": "master", "schema": "2.0.0", "ceiling_dbtp": -1.0, "expected_gain_reduction_db": 2.0},
  {"action": "send_back", "target": "master", "schema": "2.0.0", "reasons": ["already_mastered:measured"]}
]
```

### Applying it: `adapter/`

The recipe is DAW-neutral; `adapter/` translates it into **verified** `mcp_logic` calls, read from
its source: `logic_plugins.set_eq_band_verified`, `set_param_verified`, `insert_verified`, and
`logic_mixer.set_volume/set_pan`.

```bash
python -m adapter.cli --recipe recipe.json --project /path/Session.logicx \
    --tracks "vocal=3:Lead Vocal,drums=1:Drums" --master "0:Stereo Out"   # dry run
# add --apply to post to http://127.0.0.1:8765/mcp
```

| Action | Status | Calls it emits |
|---|---|---|
| `fader` | **ready** | `logic_mixer.set_volume` (`dB → 0..1`, attenuation only) |
| `pan` | **ready** | `logic_mixer.set_pan` |
| `eq.bell` | **ready** | `set_eq_band_verified` ×3: `Frequency`/`Hz`, `Gain`/`dB`, `Q-Factor`/`Q` |
| `eq.high_pass`, `eq.high_pass_stem` | **ready** | `set_eq_band_verified` on `Low Cut`, `Frequency`/`Hz` (slope is a host popup → manual) |
| `eq.shelf` | **ready** | `set_eq_band_verified` on `Low Shelf`/`High Shelf` |
| `compressor` | **ready** | `insert_verified` + `set_param_verified` for `ratio`, `attack`, `release` |
| `limiter` | **ready** | `insert_verified` + `set_param_verified` `outputCeiling`/`dB` |
| `polarity.invert` | **ready** | `insert_verified` Gain + `set_param_verified` `phaseInvert` |
| `de_esser`, `dither`, `stereo.width`, `dynamic_eq` | **unsupported** | not in the verified catalog; the step carries the manual instruction |
| `send_back` | **terminal** | nothing is applied |

Every verified write carries `mode: duplicate_applyback` and `project_expected_path`;
`insert_verified` also carries `expected_name` (a bare track index is refused by design).
Compressor `threshold` is normalized 0–100 in that surface — **not dB** — so it is left manual.
`--apply` is off by default and every call reports applied / error.

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

`0.4.0` (rules 2.0.0). Measurement, gates, sections, references and the recipe contract are in place,
with CI on 3.9/3.11/3.12. What is still missing is tracked in [`docs/LIMITS.md`](docs/LIMITS.md):

* **denoise, dynamic EQ, tonal shelves** — vocabulary without an action;
* **the adapter covers volume, pan, Channel EQ (Hz/dB/Q), compressor ratio/attack/release, the
  limiter ceiling and polarity** — de-esser, dither and stereo imaging have no verified surface;
* **the mix half is thin** — no gain staging, phase-before-EQ, HPF policy or panning;
* **confidence policy** — answers below a threshold still produce a recipe instead of a refusal;
* **no human-labelled evaluation** — sensitivity is measured, agreement with an engineer is not.

## License

MIT.
