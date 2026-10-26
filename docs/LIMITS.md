# Limits

What this server does **not** do, stated plainly, so nobody mistakes it for an engineer.

## It does not listen

There is no ear here. It reads numbers computed by DSP. Everything it can say is a function of
those numbers, which means:

* it cannot judge translation across systems (car, phone, club) — the core of mastering;
* it cannot hear punch, groove, width, depth, or whether the vocal "sits";
* it cannot prefer one take over another;
* it cannot be surprised. If the measurement does not capture a problem, the problem does not exist.

Not decided automatically: denoise, dynamic EQ, tonal shelves (the adapter can apply a hand-written
`eq.shelf`, but the decision model does not emit one). The adapter applies volume, pan and the
verified Channel EQ (`Frequency`/`Gain`/`Q` on Low Shelf, Peak 1–4 and High Shelf) in engineering
units. Limiter, compressor ratio/attack/release, polarity, high-pass, de-esser, dither and stereo
imaging are **manual**: `mcp_logic` has no verified surface for them, so the adapter emits no call,
and the always-manual steps are listed in the plan with the reason.

`adapter/loop.py` closes export → measure → compare → iterate, but it only iterates **track** EQ.
Faders, pan and shelves are applied once (before the stage bounce); master EQ is advisory because
its band state cannot be read back; the measured effect of a bell is not the transfer function of
the plug-in, so a correction that does not transfer stops the loop instead of being forced. A
generated master recipe normally contains `eq.high_pass` and `limiter`, so the loop ends with exit
`2` (`manual_delivery`/`partial_manual`) rather than claiming delivery.

`export_run` drives the native bounce, but it does not pin dither or bit depth. The loop uses
`bounce_settings` equality plus sample rate, channel count and duration as evidence that two bounces
are comparable; that is not a delivery guarantee, and the bounce dialog is never touched.

Per-section analysis, reference-track deviation and the correlation drop after the chain **are**
implemented (`analyze_sections`, `analyze_reference`, `judge_master.previous_path`).

## Measurement approximations

| Metric | Reality |
|---|---|
| `integrated_lufs` | `pyloudnorm` when installed; otherwise a K-weighting + RMS approximation, ±0.5 LU |
| `true_peak_dbtp` | 4× oversampled (BS.1770 minimum). Dedicated meters use 8× — expect ±0.3 dB |
| `loudness_range_lu` | short-term P95−P10, indicative |
| `phase_correlation` | one number for the whole file; it does not localize |
| masking matrix | average band overlap, not perceptual masking |
| noise floor | measurable **only inside a real gap**; returns `None` on continuous music |
| lossy detection | a cliff test at 16 kHz; a 22 kHz cut on a 44.1 kHz file still looks "full band" |
| stem gates | need named stems (`vocals`, `drums`, `bass`); an unnamed export cannot be role-matched |
| reference curve | measured from the file when `reference_path` is given; a normalized **constant** otherwise |
| sibilance | 5–9 kHz energy relative to full-band; a descriptor, **not** a de-esser threshold |
| limiter gain reduction | a **prediction** from measured LUFS/true peak, not a measurement of the limited file; it assumes the peak lands on the ceiling, and the real GR depends on the material |
| pre-limiter trim | up to 3 dB of input trim; beyond 4 dB GR + 3 dB trim (7 dB over the ceiling) the recipe is a terminal `send_back`, not a request to limit harder |

## It does not know the music

Genre, intent and arrangement are only what you put in `state.context`. A dub mix and a mistake can
look identical to a band-energy vector.

## Jev specifics

* It decides over the state it is given. Thin state → thin judgement.
* Its `score` scale is 2–10 levels; it saturates. In our tests "muddy" hit the ceiling on already-dark
  material and stopped responding.
* Its confidence is calibrated by design, but **not validated for this domain yet**. Do not threshold
  on it until you run a labelled set.
* Network call per judgement (~0.3 s, ~$0.0002). Cache identical states.

## Validation status

24 clips, 8 tracks × 3 versions, one repetition, **no human labels**. It shows sensitivity to a known
perturbation, not agreement with a trained ear. A proper evaluation needs 30+ clips, two perturbation
levels, human ratings, and a reliability diagram.

## What a real engineer does that this doesn't

Listens holistically → compares against references on multiple systems → decides with the ear,
using meters as guardrails → iterates. This server covers the meters and the decision scaffolding;
the ear is still yours.
