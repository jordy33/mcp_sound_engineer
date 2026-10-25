# Architecture

`mcp_sound_engineer` is the **decision layer**. `mcp_logic` is the **execution layer**.

```
Logic Pro session
   │  (stems / stereo out)
   ▼
mcp_logic ── exports audio ──► mcp_sound_engineer
                                   │  analysis.py  → facts (deterministic)
                                   │  jev.py       → answers (typed, calibrated)
                                   │  rules.py     → recipe (capped by the craft)
   ◄──── recipe (JSON) ────────────┘
   │
   ▼  apply: Channel EQ, Compressor, Limiter, Dither
```

## Why two layers

A generative model can describe a mix; it cannot be trusted to *decide* one at 200 ms per call
inside an agent loop, and it cannot guarantee a valid plugin/parameter pair. Jev returns typed
answers (`choice` / `score` / `noul`) with probabilities, so the recipe is always well formed.

## The three outputs of every judgement

1. `facts` — measured, reproducible, no AI.
2. `answers` — typed decisions with confidence.
3. `recipe` — ordered Logic actions with numeric parameters, clipped to the craft's caps.

## Hard caps (non-negotiable)

* corrective EQ: ≤ 3 dB per point, ≤ 6 dB cumulative
* broadband compression: ≤ 3 dB gain reduction, ratio ≤ 3:1
* limiter: ceiling −1.0 dBTP, ≤ 4 dB gain reduction
* send back instead of processing: phase correlation < +0.3, baked-in clipping, lossy source,
  noise floor > −50 dB

If a decision would exceed a cap, the recipe does not include it — the agent must go back to the mix.
