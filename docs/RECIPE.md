# Recipe schema 2.0.0

A recipe is an ordered array of actions. Order matters: it is the signal chain.

```json
{"action": "eq.bell", "target": "master", "schema": "2.0.0", "freq_hz": 300, "gain_db": -2.0, "q": 5.0, "role": "corrective"}
```

## Common fields

| Field | Required | Notes |
|---|---|---|
| `action` | yes | one of the vocabulary below |
| `target` | yes | `master`, `mix_bus`, `bus`, `vocal`, `drums`, `bass`, `stem` |
| `schema` | yes | `1.0.0` |
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
| `limiter` | `ceiling_dbtp` ≤ −1.0, `expected_gain_reduction_db` ≤ 4, `target_lufs` |
| `fader` | `target`, `gain_db` (mixing, ≤ ±3) |
| `pan` | `target`, `position` −1..1 |
| `dither` | `bit_depth` 16 or 24, `type` (`tpdf`) — only for fixed-bit destinations |
| `send_back` | `reasons` (array of measured causes) — terminal |

## Adapter obligations

1. Reject unknown actions and out-of-range parameters loudly.
2. Apply in order; report per-action status (`applied`, `clipped`, `unsupported`).
3. Support `dry_run` (plan only, no change).
4. Never invent parameters that the recipe did not carry.
