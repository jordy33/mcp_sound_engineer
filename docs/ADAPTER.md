# Adapter

`adapter/` turns a recipe into actions. It is a client, not an MCP server: it speaks JSON-RPC to
`mcp_logic`, which owns Logic Pro. The design rule is simple — **if `mcp_logic` does not verify it,
the adapter does not emit it**.

* Contract: `adapter/mcp_logic_contract.py`
* Pinned commit: `b42cdd37` (`2026-10-26`)
* `ADAPTER_CONTRACT_VERSION = "mcp_logic@b42cdd37+2026-10-26"`
* Recipe schema: `2.1.0` (unchanged)

`tests/fixtures/mcp_logic_contract.json` records the provenance (`commit`, date, source file and
line) for every name below. The tests assert that generated calls use only vocabulary from that
fixture; `validate_call` refuses forbidden parameter names.

## Verified surface

Every `mcp_logic` tool is a command tool: `arguments = {"command": ..., "params": {...}}`.

| Tool | Command | Used for |
|---|---|---|
| `logic_plugins` | `get_inventory` | live slot/plugin inventory for a track or the output strip |
| `logic_plugins` | `insert_verified` | insert a verified-empty slot (mcp_logic allows Gain / Channel EQ / Compressor; the adapter only inserts Channel EQ) |
| `logic_plugins` | `set_eq_band_verified` | Channel EQ `Frequency` (Hz), `Gain` (dB), `Q` (Q) |
| `logic_plugins` | `get_channel_eq_state_verified` | track Channel EQ readback by `target_ref` |
| `logic_mixer` | `set_volume`, `set_pan` | faders and pan |
| `logic_project` | `export_plan`, `export_run` | bounce export used by the loop |
| `logic_audio` | `analyze_file`, `compare_spectra` | read-only measurement/analysis |

Addressing rules:

* **Track**: `insert_verified` takes `track` + `expected_name`. After the insert, the inventory is
  re-read and parameter writes bind to the slot's `plugin_insert_ref`. `set_eq_band_verified` takes
  `insert` + `target_ref` and never `expected_name`.
* **Master**: the Stereo Out strip has no Arrange index and emits no `plugin_insert_ref`. It is
  addressed as `strip: "output"` (plus `expected_name` on the static contract, which the strip path
  ignores) and `insert`. An index in `--master "0:Stereo Out"` is informational only.
* Inserts always go to the first slot whose `read_status == "empty"`. A builder cannot hard-code a
  slot; it must come from the inventory. No inventory means `needs_inventory`, never slot 0.
* A pre-existing track Channel EQ is read first and writing to it requires `--overwrite-eq`.
  A pre-existing master Channel EQ is never reused or overwritten: the output strip's state is
  unreadable, so the step is `manual` with `master_eq_state_unreadable`.

### Capability matrix

| Action | Status | `mcp_logic` call | Notes |
|---|---|---|---|
| `fader` | `needs_calibration` → `ready` | `logic_mixer.set_volume` | contract 0..1 with a measured taper, not dB |
| `pan` | `ready` | `logic_mixer.set_pan` | |
| `eq.bell` | `ready` | `logic_plugins.insert_verified` (Channel EQ) + `logic_plugins.set_eq_band_verified` ×3 | once per target; `Frequency`/Hz, `Gain`/dB, `Q`/Q |
| `eq.shelf` | `ready` | insert + `set_eq_band_verified` ×2–3 | `Frequency`/`Gain` (+ `Q` when present) on Low/High Shelf; sign preserved |
| `eq.high_pass`, `eq.high_pass_stem` | `manual` | none | Low Cut is observed-only; slope is a host popup |
| `compressor` | `manual` | none | the insert is verified, but ratio/attack/release have no verified readback; threshold/`limiter_on`/`auto_release` alone are not the recipe |
| `limiter` | `manual` | none | named in `insert_verified`'s tool text but excluded by the live `insertableAllowlist`; even if inserted, ceiling has no verified write |
| `polarity.invert` | `manual` | none | Gain is insert-only; `phaseInvert` has no write/readback |
| `de_esser`, `dither`, `stereo.width`, `dynamic_eq` | `manual` | none | no verified surface; dither is a bounce setting |
| `send_back` | `terminal` | none | terminal recipe |

`Q-Factor`, `outputCeiling`, `phaseInvert`, `ratio`, `attack` and `release` are forbidden emissions.

A `manual` step is not a fallback an LLM may replace with a guessed call. `build_plan` emits it with
`apply_by: "human"` and a `hint`, `apply_steps` sends only `ready` steps, and any action missing
from this matrix defaults to `manual`. The loop reports pending manual work with exit `2` instead
of claiming delivery.

## Honest Contract

All `logic_plugins.*` calls use Honest Contract v2 (`hc_schema: 2`); `logic_mixer.*` uses v1.

A call is accepted only when the transport and JSON-RPC succeed, `isError` is false,
`success is true`, `verified is true`, `state == "A"`, and for `logic_plugins.*`, `hc_schema == 2`.

* **State A** — verified. The operation is reported `applied`.
* **State B** — accepted but not verified (`reason`). The run stops; it is not `applied`.
* **State C** — error, possibly with `write_attempted: true`. The run stops and reports `partial`;
  a mutation may exist.

There are no write retries and no rollback. After a State C, the adapter does not continue writing
the chain. An insert that is not State A blocks every parameter write that depends on it. A single
inventory refresh after an insert (short polling, read-only) is allowed to find the new slot; the
write itself is never retried.

`apply_steps` records per call: step, call, operation, `hc_schema`, `state`, `verified`, `reason`,
`error`, `write_attempted` and the full payload.

## Planning states

`ready`, `manual`, `needs_calibration`, `needs_inventory`, `unresolved`, `terminal`.

The CLI and the loop call `build_plan`, which runs `rules.validate_recipe` and checks
`action["schema"] == RECIPE_SCHEMA_VERSION` before planning. Manual steps carry `apply_by: "human"`
and a `hint` explaining exactly what a person has to do. A plan with any `unresolved`,
`needs_inventory` or `needs_calibration` step is blocked for the loop: it does not perform a partial
apply.

## Loop

`python -m adapter.loop` closes apply → export → measure → compare → iterate. It is a **dry run**
unless `--apply` is passed; without it, no directory is created, no export is run, no write happens
and no report is written.

Layout under an existing `--output-root`:

```
<output-root>/<run-id>/baseline/
<output-root>/<run-id>/stage/      only when static moves exist
<output-root>/<run-id>/iter-1/
<output-root>/<run-id>/iter-2/     up to --max-iterations
```

`--run-id` defaults to a local timestamp and must match `^[A-Za-z0-9._-]+$`. Every path is resolved
with `realpath` and must remain under the run root; an escape is `output_root_escape`.

Phases:

1. **Baseline.** Either an exported bounce (`export_plan` → `export_run` with strict `confirmed:
   true`) or `--before` plus `--trust-before`. The external file is copied byte-for-byte under the
   run root, hashed, and analysed there; the original is never modified. `--before` without
   `--trust-before` stops with `trusted_before_required` before exporting.
2. **Static.** Faders, pan and shelves are applied once. If any exist, a `stage` bounce is measured
   and becomes the base for the bells, so the bell residual does not absorb the static movement.
   No bells → final result is `static_only`.
3. **Bells.** Channel EQ gains are applied and exported to `iter-N`.
4. **Compare.** `analyze_master` measures the artifact in-process; `logic_audio.compare_spectra`
   (`comparison_mode: "spectral_shape"`, 1/6-octave grid) yields the residual per band. Every
   artifact's SHA-256 is computed locally and must equal the `compare_spectra` fingerprint, or the
   loop stops with `artifact_changed`.

Export predicates are exact: `status == "completed"`, one project, one verified bounce, zero
skipped/uncertain/failed, project identity and open verified, artifact State A with
`bounce_fired`, and path under the run root. `unsupported_or_blocked_steps` is allowed to contain
`cloud_delivery`; anything else is `plan_blocked`.

### Correction rule

The effect window of a bell is the FWHM approximation `|log2(center/freq)| <= 0.72/q` octaves
(nearest grid band if none fall inside). For iteration *n*:

```
Δ            = G_cur - G_prev                        (gain increment actually written)
E_inc        = mean(residual in window), prev → n    (measured incremental effect)
deviation    = Δ - E_inc
G_new        = G_cur + clamp(deviation, ±max_correction_db)
```

A band is corrected only when **all** hold:

* no manual limiter/compressor on that target or on master;
* State A readback and the band is verified-enabled (track);
* its window does not overlap another correctable bell on the same target;
* `|Δ| >= 0.5 dB` and `|E_inc| >= 0.3 dB`;
* `0.25 <= |E_inc| / |Δ| <= 1.5` (the transfer gate);
* `sign(E_inc) == sign(Δ)`;
* `|deviation| > --incremental-tolerance-db`.

Otherwise the disposition is `no_change`, `no_measurable_effect`, `residual_low_trust`,
`sign_mismatch` or `effect_mismatch`. A low-trust disposition stops the loop **before** numeric
convergence is declared.

Caps: `--max-correction-db` (1.0), the bounded `--max-cumulative-correction-db` (3.0) relative to
the recipe gain, and the rule caps 3 dB per point and 6 dB cumulative → `correction_cap`.

Master EQ is never corrected: its measured effect is `master_eq_advisory`. A tracked bell whose
readback is exact but whose spectrum effect cannot be measured stops as `readback_confirmed`.
`--no-improvement-db` and `--max-iterations` bound the loop. Faders, pan and shelves are never
iterated.

### Comparability and guards

Every artifact must share the baseline `sample_rate` and `channel_count`, and its duration must be
within `max(0.25 s, 0.5 %)`. The artifact must not be silent (`silence_ratio < 0.95`, peak above
−60 dBFS), clipping must not increase, stereo phase correlation must stay ≥ 0.3 and not drop more
than 0.15, and sibilance must not worsen by more than 1.0 dB. `bounce_settings` must be readable
(`read_status == "ok"`) and identical across bounces unless
`--allow-unverified-bounce-settings` is passed; the first bounce is the reference.

If the recipe carries `limiter.ceiling_dbtp`, a true peak above `ceiling + 0.1 dB` sets
`manual_delivery`: the limiter is manual, so delivery is never claimed and no EQ compensation is
attempted.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | verified convergence, static-only, readback-confirmed, terminal recipe, or a clean dry run |
| `2` | honest stop: manual work pending (`manual_only`, `manual_delivery`, `partial_manual`), a limit (`max_iterations`, `no_improvement`, `correction_cap`) or low trust (`sign_mismatch`, `effect_mismatch`, `residual_*`, `master_eq_advisory`, `band_enablement_unverified`) |
| `1` | failure: `trusted_before_required`, `plan_blocked`, `output_root_escape`, `export_failed`, `state_not_verified`, `dependency_skipped`, `artifact_*`, `bounce_settings_*`, `analysis_failed`, `guardrail_failed`, `recipe_invalid` |

The report (`--report`, written only with `--apply`) has schema
`mcp_sound_engineer.loop.v1`: run id, project, recipe hash, contract version, baseline provenance,
every exported artifact (including one rejected by a guard), operations with State, export
counters, per-iteration metrics and guards, per-bell windows and deviations, manual actions,
`stop_reason`, `warnings` and `exit`.

### Safety

* Dry run by default; `--apply` is required for directories, exports, writes and the report.
* `export_run` is only called after an `export_plan` that satisfies the predicate, with
  `confirmed: true`.
* The master is never a blind write target: a new Channel EQ only on a verified-empty output slot,
  no reuse, no readback, no correction.
* No rollback, no write retries, no chain reordering, no plugin deletion.
* Manual limiter/compressor in the recipe prevents bell auto-correction; delivery stays open.

## Manual live probe

`adapter/live_check.py` is **never run from `--test` or CI**. It requires Logic open and a
disposable scratch project. Without `--write` it only reads (health, inventories, track Channel EQ
state when a `plugin_insert_ref` exists, `export_plan`). With `--write --confirm` it requires
exactly one track, rejects an existing Channel EQ, inserts one into a verified-empty slot, writes
Peak 1 `Frequency`/`Gain`/`Q`, and requires the gain readback within ±0.1 dB. The output strip is
never a write target. A State C with `write_attempted: true` may have left a mutation; the tool says
so and does not retry.
