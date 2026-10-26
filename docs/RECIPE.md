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
| `send_back` | `reasons` (array of measured causes) — terminal |

### Limiter

The gain reduction comes from **two** constraints, not one:

```
g_loud = 0 if desired_lufs is None else max(0, desired_lufs - lufs)
g_max  = ceiling_dbtp + 4 - true_peak
input_gain_db                  = min(g_loud, g_max)
expected_gain_reduction_db     = max(0, true_peak + input_gain_db - ceiling_dbtp)
```

* `input_gain_db` is the **control** the adapter writes. `expected_gain_reduction_db` is a
  **prediction** and must not be written to a plug-in parameter.
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

1. Reject unknown actions and out-of-range parameters loudly (`validate_recipe` already does).
2. Apply in order. Planning statuses are `ready`, `needs_calibration`, `unresolved`,
   `unsupported` and `terminal`; execution results are `applied` or `error`.
3. Support `dry_run` (plan only, no change).
4. Never invent parameters that the recipe did not carry.
5. Write `input_gain_db` as the limiter input gain; never write the GR prediction.

## Migration 2.0.0 → 2.1.0

Limiter actions must add `input_gain_db` (use `0.0` for the old peak-only behavior) and keep
`expected_gain_reduction_db`. `desired_lufs` is optional; without it the limiter is peak-only.
Recipes that omit `input_gain_db` are rejected by `validate_recipe` and must be regenerated.
