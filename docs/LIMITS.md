# Limits

What this server does **not** do, stated plainly, so nobody mistakes it for an engineer.

## It does not listen

There is no ear here. It reads numbers computed by DSP. Everything it can say is a function of
those numbers, which means:

* it cannot judge translation across systems (car, phone, club) — the core of mastering;
* it cannot hear punch, groove, width, depth, or whether the vocal "sits";
* it cannot prefer one take over another;
* it cannot be surprised. If the measurement does not capture a problem, the problem does not exist.

Not implemented yet: denoise, dynamic EQ, tonal shelves, and an adapter that applies the recipe
to a real session. Per-section analysis, reference-track deviation and the correlation drop after
the chain **are** implemented (`analyze_sections`, `analyze_reference`, `judge_master.previous_path`).

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
| reference curve | a **constant**, not a measured commercial reference (see roadmap) |

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
