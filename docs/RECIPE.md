# Recipe schema 2.1.0

A recipe is an ordered array of actions. Order matters: it is the signal chain.

```json
{"action": "eq.bell", "target": "master", "schema": "2.1.0", "freq_hz": 300, "gain_db": -2.0, "q": 5.0, "role": "corrective"}
```

## Common fields

| Field | Required | Notes |
|---|---|---|
| `action` | yes | one of the vocabulary below |
| `target` | yes | `master`, `mix_bus`, `bus`, `vocal`, `drums`, `bass`, `stem` |
| `schema` | yes | `2.1.0` |
| `stem` | for stem-targeted moves | exported basename/path; the adapter resolves it before a role key |
| `reason` | no | human-readable justification |
| `role` | no | `corrective` or `tonal` (counts toward the 6 dB cap only if corrective) |

## Vocabulary

| action | parameters |
|---|---|
| `eq.high_pass` | `freq_hz` 20–30, `slope_db_oct` 6–12 |
| `eq.bell` | `freq_hz`, `gain_db` (corrective ≤ −3), `q` 4–8 corrective / 0.5–1.5 tonal |
| `eq.shelf` | `freq_hz`, `gain_db`, `q` 0.5–1.5 |
| `compressor` | `ratio` 1.5–3.0, `attack_ms` 10–30, `release_ms`, `gain_reduction_db` ≤ 3 |
| `dynamic_eq` | `freq_hz`, `threshold_db`, `ratio` |
| `de_esser` | `freq_hz` ~5–9 k, `ratio` |
| `stereo.width` | `mono_below_hz`, `width_db` ≤ 2 |
| `limiter` | `ceiling_dbtp` ≤ −1.0, `input_gain_db` ≥ −3, `expected_gain_reduction_db` ≤ 4, optional `desired_lufs`, informational `loudness_reached` / `approx_lufs_after` / `plr_db` |
| `fader` | `target`, `gain_db` (mixing, ≤ ±3), `stem` for a per-stem move |
| `pan` | `target`, `position` −1..1 |
| `dither` | `bit_depth` 16 or 24, `type` (`tpdf`) — only for fixed-bit destinations |
| `send_back` | `reasons` (array of measured causes, or `unmeasured:<field>`) — terminal |

### Limiter

The gain reduction comes from **two** constraints, not one:

```
g_loud = 0 if desired_lufs is None else max(0, desired_lufs - lufs)
g_max  = ceiling_dbtp + 4 - true_peak
input_gain_db                  = min(g_loud, g_max)
expected_gain_reduction_db     = max(0, true_peak + input_gain_db - ceiling_dbtp)
```

* `input_gain_db` is the **control** a human applies at the limiter. The adapter does not write
  it: the Limiter has no verified-writable parameter in `mcp_logic`, so the step is planned as
  `manual`. `expected_gain_reduction_db` is a **prediction** and is never written to a plug-in
  parameter.
* `desired_lufs` is optional and is **not** a delivery target. Without it the limiter is
  peak-only, which is the recommended mode for streaming (playback normalization already turns
  a hotter master down).
* `loudness_reached` is vacuous (`true`) when `desired_lufs` is absent; it only reports whether
  an explicitly requested loudness was met.
* `approx_lufs_after` is a prediction (`lufs + input_gain_db`), not a measurement.
* `plr_db` is `true_peak - lufs`, the peak-to-loudness ratio used to diagnose an unreachable
  request.
* A negative `input_gain_db` is a pre-limiter trim: the peak exceeds the ceiling by more than
  the 4 dB GR budget. Beyond 4 dB GR + 3 dB trim, the recipe is a terminal `send_back`
  (`peak_beyond_limiter`).

## Adapter obligations

1. Validate loudly before emitting anything: `rules.validate_recipe` for actions and ranges, and
   the recipe's `schema` must equal the `RECIPE_SCHEMA_VERSION` the adapter implements. A bad
   recipe produces an actionable error, never a partial plan.
2. Apply in order. Planning statuses are `ready`, `manual`, `needs_calibration`, `needs_inventory`,
   `unresolved` and `terminal`; execution results are `applied`, `error`, `skipped` and `partial`.
3. Support `dry_run` (plan only, no change). The adapter CLI and loop write nothing unless
   `--apply` is given.
4. Never invent parameters that the recipe did not carry.
5. Only emit what `mcp_logic` verifies: mixer volume, pan, and Channel EQ
   `Frequency`/`Gain`/`Q`. A write is `applied` only in Honest Contract State A
   (`success`, `verified`, `state: "A"`, and `hc_schema: 2` for `logic_plugins.*`). State B/C
   stops the run; there is no retry and no rollback.
6. `limiter`, `compressor`, `polarity.invert`, `eq.high_pass`, `de_esser`, `dither`,
   `stereo.width` and `dynamic_eq` are `manual` and emit no call. The limiter's
   `input_gain_db`, `ceiling_dbtp` and GR prediction stay instructions for a human.
7. `send_back` is terminal: nothing is applied and the reasons are surfaced.
8. Missing live inventory is `needs_inventory`; the adapter never assumes an empty slot 0.

## Migration 2.0.0 → 2.1.0

Limiter actions must add `input_gain_db` (use `0.0` for the old peak-only behavior) and keep
`expected_gain_reduction_db`. `desired_lufs` is optional; without it the limiter is peak-only.
Recipes that omit `input_gain_db` are rejected by `validate_recipe` and must be regenerated.
