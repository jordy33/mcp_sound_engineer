# mcp_sound_engineer

An MCP server that **measures audio and decides like a mix/master engineer**, returning a
DAW-neutral action plan.

It does not control any DAW. It measures and decides; a separate adapter applies the plan. That
adapter is **code in this repository** ([`adapter/`](adapter/)), not an LLM improvising calls:

```
┌───────────────────────┐   facts + answers + recipe   ┌──────────────────────────┐
│  mcp_sound_engineer   │ ───────────────────────────► │  adapter/ (this repo)    │
│  measure → judge      │        (neutral JSON)        │  recipe → mcp_logic calls│
└───────────────────────┘                              └────────────┬─────────────┘
        numpy + Jev                                                │ JSON-RPC
                                                     ┌─────────────▼─────────────┐
                                                     │  mcp_logic (DAW MCP)      │
                                                     │  Logic Pro                │
                                                     └───────────────────────────┘
```

Two hats: **mixing** (N stems: balance, masking, corrective EQ) and **mastering** (one stereo file:
send-back gates, dynamics, limiter, playback-normalization reference, dither).

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
# required: the key from step 2
JEV_API_KEY=apikey_...

# optional: override the endpoint chosen from the key prefix
JEV_ENDPOINT=https://api.typesafe.ai/v1/systemone

# optional: model id
JEV_MODEL=jev-latest
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

Register it in any MCP client next to the DAW MCP (`mcp_logic`) — they are separate servers. The
translation from recipe to calls is `adapter/`, a client of `mcp_logic`, not another server:

```json
{"mcpServers": {
  "engineer": {"command": "/abs/path/mcp_sound_engineer/start-sound-engineer-mcp.sh", "args": ["--stdio"]}
}}
```

## 4. Tools

| Tool | Input | Returns |
|---|---|---|
| `analyze_master` | one stereo file | LUFS, true peak, LRA, crest, phase correlation, mono fold-down loss, 6 bands, sibilance, centroid, measured lossy cutoff, clipping and flat-topped frames; mono/stereo/5.1 layouts |
| `analyze_stems` | N files | per-stem loudness, peak, bands, sibilance, low-band correlation, time-weighted pairwise overlap while both stems are active (a spectral-crowding proxy, not perceptual masking), and stem gates (buried vocal, outlier element, mono bass) |
| `analyze_sections` | path + window | per-window LUFS/bands/correlation, deviation from the median (informational), and a partial final window when the tail is ≥5 s |
| `analyze_reference` | path | a commercial reference's band balance and sibilance, to feed `judge_master` |
| `validate_recipe` | recipe | adapter-side contract check: caps, schema, action ids, `expect` shape, limiter control fields, terminal `send_back` |
| `verify_recipe` | recipe + before/after files (optional stem lists) | per-action `expect` results after a bounce: `met`, `missed` or `unmeasured` — never a claim the measurement did not support |
| `judge_master` | path, platform, optional `reference_path` / `previous_path` / `desired_lufs` | `answers` + `recipe` + `warnings`; `desired_lufs` is an optional loudness request, not a delivery target (omit it for peak-only limiting); **measured gates run before the model**, so a refusal needs no key |
| `judge_mix` | stem paths | `answers` + `recipe`; the masking bell uses the measured overlap pair and band, not a fixed 300 Hz |
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
| Missing gate input | any of phase, mono fold-down loss, clipping/flat-top count, duration, sample rate, LRA/TP/LUFS, bandwidth | `evaluate_gates` | **not measured** → `unmeasured:<field>` and `send_back`; no healthy defaults |
| Clipping that blocks mastering | ≥ 500 frames **and** ≥ 0.01 % | `MIN_CLIPPED_*` | **measured** → `send_back`; uses the larger of hard-clip frames and flat-topped runs (a ceiling below full scale); `count` is stereo frames, so the denominator is duration × sample rate |
| True peak above full scale | > 0 dBTP | `evaluate_gates` | **measured** → `send_back`; inter-sample peaks distort on lossy encodes and DACs |
| Already a finished master | LRA < 4 **and** TP > −1 **and** LUFS > −12 | `MAX_LRA_FINISHED` | **measured** → `send_back` (product rule) |
| Corrective EQ per point | score 2–10 maps to 0–4 dB; ≤ 3 dB applied | `GATE.max_corrective_db_per_point`, `map_score` | only the extreme score exceeds the cap → `send_back`, never clamped |
| Cumulative corrective EQ | ≤ 6 dB | `GATE.max_cumulative_corrective_db` | includes the sibilance cut |
| Masking (mix) | score 2–10 maps to a 0–3 dB cut, below 2.5 no cut | `MASKING_*`, `GATE.max_corrective_db_per_point`, `map_score` | frequency and stem pair come from the time-weighted co-activity pairs; stems that never sound together get no bell; sub/bass-only overlap emits no bus bell; no measurement means no invented frequency; `mix_warnings` names the structural remainder at the cap |
| Upward expansion | not offered | `dynamics` choices / `master_warnings` | over-compressed material is an upstream problem; no action is emitted and the warning says so |
| Total gain reduction | ≤ 6 dB (source range 6–8) | `GATE.max_total_gain_reduction_db` | compressor + limiter summing over budget → `send_back` |
| Mono compatibility | fold-down loss < 6 dB | `GATE.min_mono_cancel_db` | **measured** → `send_back`; missing measurement is `unmeasured`, never a healthy 0 dB |
| Gain staging (mix) | every stem toward −18 LUFS | `GAIN_STAGING_*` | deterministic faders, ±6 dB max; a capped move or unmeasured loudness is warned |
| Phase before EQ (mix) | correlation < +0.3 | `phase_before_eq` | `polarity.invert` or `eq.high_pass_stem` |
| Confidence | < 0.5, or absent | `CONFIDENCE_MIN` | `confidence.low` / `confidence.unknown` + warning; a missing confidence is not assumed to be 1.0 and the recipe is a draft |
| Broadband compression | 1.5–3:1, 10–30 ms, 1–3 dB GR | `CHAIN`, `GATE.min/max_gain_reduction_db` | emitted on the master bus |
| Dynamics amount | abstract score 2–10 | `map_score` | linear 2→1 dB, 6→2 dB, 10→3 dB; outside the scale clamps, never interpreted as dB |
| De-esser | after compression | `CHAIN` | emitted after the compressor |
| Limiter | GR = max(0, tp + input_gain − ceiling); ceiling from the destination | `plan_limiter` | input gain = min(loudness request, peak budget); `expected_gain_reduction_db` is a prediction, not a control |
| Pre-limiter trim | ≤ 3 dB; more than 7 dB over the ceiling → `send_back` | `GATE.max_limiter_input_trim_db` | a peak that 4 dB GR + 3 dB input trim cannot fix is not a mastering problem |
| Dither | only when the destination needs fewer bits than the source | `PLATFORMS[...]`, `facts.bit_depth` | none for streaming; TPDF for CD/podcast only if reducing |
| Playback normalization | Spotify −14, Apple −16, … | `PLATFORMS` | informational; never a delivery target |
| Unknown destination | — | `loudness_target` | raises, no silent fallback |
| Reference curve | measured from a real track when `reference_path` is given, generic normalized default otherwise | `analyze_reference`, `REFERENCE_CURVE` | default six-band linear sum = 1.0; `judge_master` warns and reports `reference_source: "generic_default"` because a bass-heavy genre can read as excess; a partial measured curve fails loud |
| Correlation drop after the chain | > 0.15 | `correlation_drop` | warning when `previous_path` is given; missing correlation on either side is reported unmeasured, not as a zero drop |
| Sibilance | 5–9 kHz relative to full-band energy | `analysis.SIBILANCE_BAND` | measured as `spectral.sibilance_db_rel`; informs the question, not a de-esser threshold |
| Noise floor | > −50 dB; minor between −60 and −50 | `GATE.noise_floor_moderate_db`, `GATE.noise_floor_minor_db` | **measured in silence only**; > −50 → `send_back`, > −60 → warning; `None` when the file has no gap |
| Lossy source | measured cliff > 20 dB in adjacent 1 kHz bands above 10 kHz, up to Nyquist | `GATE.lossy_shelf_drop_db` | **measured** (suspected flag or measured drop) → `send_back`; the reported cutoff is the lower edge of the cliff |
| `eq.shelf`, `dynamic_eq`, `stereo.width`, `pan` | vocabulary only in the **decision model** | `ACTIONS` | ❌ not emitted by `build_master_recipe`/`build_mix_recipe`; a hand-written `eq.shelf`/`pan` is still applicable by the adapter |

**The decision model never gates.** It scores and chooses treatments; every refusal is measured.
Asking it "should this be sent back?" produced a global yes/no that contradicted its own
component scores (0.81 refuse, yet `low_end` 1.46 and `high_mid` 1.68 — both "slight").

### How to know a rule is real

```bash
./start-sound-engineer-mcp.sh --test     # 337 tests: smoke + rules + jev + analysis + adapter + loop
```

`tests/test_rules.py` asserts behaviour, plus the limiter's two-constraint arithmetic:

* every balance label lowers the offender, and a fader points at a real exported stem;
* a corrective request above 3 dB returns `send_back` instead of a clamped band, and the
  cumulative cap counts the sibilance cut (7.5 dB → `send_back`, no zero-gain bell);
* corrective bells use Q 4–8;
* 60 clipped frames in 10 s do **not** send back, 5000 do, and 5 frames in 1 s do not;
* a missing gate input is `unmeasured:<field>` and a terminal `send_back`, never a
  healthy default;
* the limiter's input gain is min(requested loudness, peak budget), and the peak-only
  default never invents input gain;
* the abstract dynamics score maps explicitly onto 1–3 dB GR (`map_score`);
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
Full schema: [`docs/RECIPE.md`](docs/RECIPE.md) (currently `2.2.0`).

```json
[
  {"id": "eq-high-pass:master", "action": "eq.high_pass", "target": "master", "schema": "2.2.0", "freq_hz": 25, "slope_db_oct": 12},
  {"id": "eq-bell:master:bass", "action": "eq.bell", "target": "master", "schema": "2.2.0", "freq_hz": 150, "gain_db": -2.0, "q": 4.0, "role": "corrective",
   "expect": {"metric": "spectral.bands_db_rel.bass", "delta_db": -2.0, "tolerance_db": 1.5}},
  {"id": "limiter:master", "action": "limiter", "target": "master", "schema": "2.2.0", "ceiling_dbtp": -1.0, "input_gain_db": 0.0, "expected_gain_reduction_db": 2.0,
   "expect": {"metric": "loudness.true_peak_dbtp", "max": -1.0, "tolerance_db": 0.1}},
  {"id": "send-back:master", "action": "send_back", "target": "master", "schema": "2.2.0", "reasons": ["already_mastered:measured"]}
]
```

`expect` is the closing measurement, not a promise: `verify_recipe` compares before/after and
returns `unmeasured` when the metric is absent. A post-bounce check can therefore fail honestly
instead of assuming the action worked.

### Applying it: `adapter/`

The recipe is DAW-neutral; `adapter/` translates it into calls that `mcp_logic` HEAD
`b42cdd37` verifies (pinned in `adapter/mcp_logic_contract.py`). Anything outside that surface
is `manual` and emits no call at all. Full contract, loop semantics and safety notes:
[`docs/ADAPTER.md`](docs/ADAPTER.md).

```bash
python -m adapter.cli --recipe recipe.json --project /path/Session.logicx \
    --tracks "vocal=3:Lead Vocal,01_bass.wav=2:Bass" --master "Stereo Out" \
    --inventory /tmp/inventory.json     # dry run against a saved inventory
# --fetch-inventory reads it live (read-only); --apply always re-reads it live
```

A `--tracks` key may be a role or an exported basename. When a recipe action carries `stem`,
the basename wins over the role key; an explicit `target: "stem"` never falls back to a role.
The Stereo Out strip has no Arrange index: it is addressed as `strip: "output"`, and an
`--master` index (`0:Stereo Out`) is accepted only as informational.

| Action | Status | What it emits |
|---|---|---|
| `fader` | **needs_calibration** | `logic_mixer.set_volume` takes a contract 0..1 with a measured taper, **not dB**: run `python -m adapter.calibrate_fader --track N` on a scratch track, then pass `--fader-calibration` |
| `pan` | **ready** | `logic_mixer.set_pan` |
| `eq.bell` | **ready** | `insert_verified` for a new Channel EQ in the first verified-empty slot (track) or `strip: "output"` (master), then `set_eq_band_verified` ×3: `Frequency`/`Hz`, `Gain`/`dB`, `Q`/`Q` |
| `eq.shelf` | **ready** | same insert, then `Frequency`/`Gain` on `Low Shelf`/`High Shelf` (+ `Q` only when the recipe carries it); the sign is preserved |
| `eq.high_pass`, `eq.high_pass_stem` | **manual** | Low Cut is observed-only; slope is a host popup |
| `compressor` | **manual** | insert-only in this surface: no verified ratio/attack/release; inserting DSP you cannot configure is an unverified side effect |
| `limiter` | **manual** | not in the insert allowlist and no verified-writable parameter |
| `polarity.invert` | **manual** | Gain is insert-only; `phaseInvert` has no write/readback |
| `de_esser`, `dither`, `stereo.width`, `dynamic_eq` | **manual** | no verified surface; the step carries the manual instruction |
| `send_back` | **terminal** | nothing is applied |

Unsupported actions are reported, never improvised. Every action in `rules.ACTIONS` has a declared
capability (`ready`, `manual` or `terminal`); a `manual` step carries `apply_by: "human"` and the
reason, emits **no** MCP call, and blocks the loop from claiming delivery (exit `2`). A new action
defaults to `manual` until the table is updated and tested. The matrix is enforced by
`tests/test_adapter.py::test_capability_matrix_is_honest` and
`::test_manual_actions_emit_no_calls_and_explain_why`.

Every plugin call carries `mode: duplicate_applyback` and `project_expected_path`;
`insert_verified` also carries `expected_name`. Mixer calls are the two-field
`{"track", "value"}` form. `set_eq_band_verified` never carries `expected_name`, and the adapter
never emits `Q-Factor`, `outputCeiling` or `phaseInvert`.

`--apply` is off by default. A call is only accepted when the Honest Contract envelope is
State A (`success`, `verified`, `state: "A"`, and `hc_schema: 2` for `logic_plugins.*`). State B
(unverified) and State C (error / `write_attempted`) stop the run; there are no write retries
and no rollback. After an insert the adapter refreshes the live inventory and binds writes to
the observed `plugin_insert_ref` (track) or the observed slot on `strip: "output"` (master).
A pre-existing track EQ needs `--overwrite-eq`. A pre-existing master EQ is never reused or
overwritten: its state is unreadable and the output strip emits no refs.

### Closing the loop: `adapter/loop.py`

`adapter/loop.py` exports a master bounce, measures it, compares it, and iterates **only** on
verified Channel EQ gains:

```bash
python -m adapter.loop --recipe recipe.json --project /path/Session.logicx \
    --tracks "vocal=3:Lead Vocal" --master "Stereo Out" \
    --output-root /path/exports --apply
```

It is a dry run without `--apply`: no directories, no exports, no writes, no report. With
`--apply` it builds `output_root/<run-id>/{baseline,stage,iter-1,iter-2}` (stage only when
faders/pan/shelves exist), exports with `logic_project.export_plan` + `export_run`, measures
in-process, and calls `logic_audio.compare_spectra` with `spectral_shape`. Every artifact is
hashed locally and its `compare_spectra` fingerprint must match, or the loop stops.

The effect window of a bell is the FWHM approximation `|log2(center/freq)| <= 0.72/q`.
`G_new = G_cur + clamp(deviation, ±--max-correction-db)` and only a track band is corrected,
read back to ±0.1 dB. Corrections are refused beyond 3 dB per point, 6 dB cumulative, or the
`--max-cumulative-correction-db` cap. Overlapping bells, manual dynamics, disabled bands and
master EQ are never auto-corrected. A tracked bell that lands but cannot be measured stops as
`readback_confirmed`; the loop does not pretend the spectrum confirmed it.

Because the limiter, compressor and high-pass are manual, a generated master recipe normally
ends with exit `2` (`manual_delivery` or `partial_manual`). The loop can verify EQ transfer; it
cannot declare final loudness/true-peak delivery. Exit codes: `0` verified convergence,
static-only, readback-confirmed, terminal recipe or a clean dry run; `2` honest stop with manual
work, a limit or low trust; `1` failure. The full stop-reason matrix is in
[`docs/ADAPTER.md`](docs/ADAPTER.md#exit-codes).

## 7. What has and has not been verified

Measured on 24 real music clips (8 tracks × 3 versions: original, +3.4 dB at 60–250 Hz, +5.3 dB at
6–16 kHz):

| Check | Result |
|---|---|
| Direction on +5.3 dB of added highs | 8/8 |
| Direction on +3.4 dB of added lows | 5/8 (both failures were scale saturation) |
| Spearman(measured tilt, "bright") | +0.77 |
| Stability across runs | ±1 point |
| End-to-end on a full 284 s track | ≈6 s per call (numpy-only: K-weighting + 4× true peak) |

**Not** verified: no human labels, one repetition per clip, synthetic perturbations. It measures
sensitivity to a known change, not agreement with a trained ear.

## 8. Status

`0.9.0` (rules 2.3.0). Measurement, gates, sections, references, the recipe contract and a
closed apply → export → measure → iterate loop are in place, with CI on 3.9/3.11/3.12. What is
still missing is tracked in [`docs/LIMITS.md`](docs/LIMITS.md):

* **the adapter emits only what mcp_logic verifies** — volume/pan and Channel EQ (a verified-empty
  insert plus `Frequency`/`Gain`/`Q`). Limiter, compressor, polarity, high-pass, de-esser, dither
  and stereo imaging are manual;
* **the loop closes EQ transfer, not loudness delivery** — with a manual limiter/compressor it
  stops honestly (exit `2`) instead of compensating with EQ;
* **master Channel EQ is advisory** — its band state cannot be read back, so it is applied once
  and never auto-corrected;
* **confidence policy** — answers below a threshold still produce a recipe instead of a refusal;
* **no human-labelled evaluation** — sensitivity is measured, agreement with an engineer is not.

## License

MIT.
