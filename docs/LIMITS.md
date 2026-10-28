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
| `integrated_lufs` | one deterministic implementation: BS.1770 K-weighting with coefficients derived for the actual sample rate (44.1/48/96 kHz agree with `pyloudnorm`/`ffmpeg ebur128` within ~0.2 LU). Long files are filtered in chunks with real pre-roll and zero padding, so the result does not depend on the chunk size. `pyloudnorm` is not used at runtime |
| `true_peak_dbtp` | 4× polyphase FIR over the whole file, edges included (BS.1770 minimum). Dedicated meters use 8× — expect ±0.3 dB on normal material, more near Nyquist |
| `loudness_range_lu` | EBU R128 / Tech 3342: K-weighted 3 s short-term loudness, −70 LUFS absolute gate, −20 LU relative gate, P95−P10. `null` when the file is shorter than 3 s |
| channel layouts | 1–2 channels use unit weights; 6 channels are assumed to be the standard WAV order FL/FR/FC/LFE/BL/BR (Ls/Rs at +1.5 dB, LFE excluded). WAV carries no layout metadata, and any other channel count raises instead of guessing |
| WAV decoding | 8/16/24/32-bit integer and 32/64-bit IEEE-float WAV, including `WAVE_FORMAT_EXTENSIBLE`, are decoded from the `fmt ` tag; non-finite samples are refused. FLAC/OGG/AIFF need the optional `soundfile` package |
| clipping | `count` is stereo frames with `|x| ≥ 0.999`; `flat_top_frames` counts frames containing a run of ≥3 equal samples at `|x| ≥ 0.5`, so a ceiling below full scale is visible. Neither knows whether the flat top came from the source or the limiter |
| `phase_correlation` | one number for the whole file; it does not localize. `mono_cancel_db` is the fold-down level change: 0 dB is mono-safe, negative is loss |
| `band_overlap_pairs` | time-weighted mean per-band minimum of relative band energy, measured only in 1 s windows where both stems are active (RMS > stem max − 40 dB): a spectral-crowding proxy, not perceptual masking. Windows can miss very brief collisions, and stems with different sample rates or too short for a window report no overlap |
| masking bell | the cut frequency comes from the measured co-activity pair and its dominant shared band; stems that never sound together get no bell; sub/bass-only overlap emits no bus bell, and no measurement means no frequency is invented |
| noise floor | measurable **only inside a real gap**; returns `None` on continuous music |
| lossy detection | an estimate from adjacent 1 kHz bands above 10 kHz over the whole-file PSD. It reports the lower edge of a >20 dB cliff up to Nyquist, so a 19 kHz cut is distinguishable from a 22 kHz one. Creative filtering or extreme noise can cause false positives; a gentle codec rolloff may not reach 20 dB and reports no cliff |
| stem gates | need named stems (`vocals`, `drums`, `bass`); an unnamed export cannot be role-matched. Buried vocal and outlier element use sustained integrated LUFS, not transient peaks |
| mix phase/width | a missing per-stem `phase_correlation` or `low_correlation` is unmeasured: no polarity/width move is emitted and `mix_warnings` names it, rather than assuming a healthy 1.0 |
| corrective source | if the model requests a correction but any band in that group was not measured, the recipe is a terminal `unmeasured:spectral.bands_db_rel` send_back instead of placing a bell at a guessed band. When the measurement shows no positive excess, no bell is emitted and the requested cut is capped at the measured excess |
| `analyze_sections` | deviation flags are **informational**: a chorus legitimately differs from the median, so a flag is not a gate. A final window shorter than the requested size is included when it lasts ≥5 s and marked `partial` |
| reference curve | measured from the file when `reference_path` is given; a generic normalized **constant** otherwise, reported as `reference_source: "generic_default"` and warned about (a bass-heavy genre can read as low-end excess). The default is normalized over the six bands; `bands_db_rel` is relative to the full spectrum, so its six-band sum can be slightly below 0 when energy exists below 20 Hz or above 16 kHz. Renormalizing only shifts a curve by a constant, which cancels when the worst band is chosen |
| upward expansion | not offered: it has no verified mapping, and an over-compressed master is an upstream problem; the recipe leaves dynamics unchanged and says so |
| post-bounce expectations | `verify_recipe` can only check the metrics that exist (`bands_db_rel`, true peak, stem LUFS, …). Compressor ratio, polarity, dither and stereo width carry no `expect`, and a missing metric is `unmeasured`, never `met` |
| sibilance | 5–9 kHz energy relative to full-band; a descriptor, **not** a de-esser threshold |
| limiter gain reduction | a **prediction** from measured LUFS/true peak, not a measurement of the limited file; it assumes the peak lands on the ceiling, and the real GR depends on the material |
| pre-limiter trim | the planner supports up to 3 dB of input trim, but `build_master_recipe` refuses any true peak above 0 dBTP first, so a normal recipe never asks the limiter to recover that much |

## It does not know the music

Genre, intent and arrangement are only what you put in `state.context`. A dub mix and a mistake can
look identical to a band-energy vector.

## Jev specifics

* It decides over the state it is given. Thin state → thin judgement.
* The state contains **file basenames, never absolute paths**: the decision service does not need
  the local user name or directory layout. `analyze_*` still returns full paths to the local client.
* Its `score` scale is 2–10 levels; it saturates. In our tests "muddy" hit the ceiling on already-dark
  material and stopped responding.
* Its confidence is calibrated by design, but **not validated for this domain yet**. Do not threshold
  on it until you run a labelled set.
* Network call per judgement (~0.3 s, ~$0.0002). Cache identical states.
* Transient 429/5xx responses, timeouts and connection failures are retried (3 attempts) under a
  45 s total deadline. A response that omits any question is rejected: a missing answer is never
  read as "nothing to fix".

## Validation status

24 clips, 8 tracks × 3 versions, one repetition, **no human labels**. It shows sensitivity to a known
perturbation, not agreement with a trained ear. A proper evaluation needs 30+ clips, two perturbation
levels, human ratings, and a reliability diagram.

## What a real engineer does that this doesn't

Listens holistically → compares against references on multiple systems → decides with the ear,
using meters as guardrails → iterates. This server covers the meters and the decision scaffolding;
the ear is still yours.
