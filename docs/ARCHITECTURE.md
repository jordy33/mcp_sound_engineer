# Architecture

Two MCP servers, two responsibilities, zero coupling.

```
                            (1) facts
Logic / REAPER / Ableton ──► mcp_sound_engineer ──► analysis.py   (numpy, offline)
        ▲                        │
        │                        │ (2) answers
        │                        ▼
        │                      jev.py  ──► Jev (typed decisions)
        │                        │
        │                        │ (3) recipe
        └──── adapter MCP ◄──────┘  rules.py (caps + neutral actions)
              (your adapter)
```

## Why not fuse them

* **Portability.** The judgement layer is DAW-agnostic; adapters are disposable.
* **Trust boundary.** Measuring is deterministic and cheap; deciding calls a network model.
  Keeping them apart lets you audit, cache and rate-limit independently.
* **Surface area.** An adapter needs one contract (the recipe schema). If they were fused, every
  new DAW would re-implement the measurement and the rules.

## Contract

The only coupling is `docs/RECIPE.md` (schema `1.0.0`). It is versioned; adapters declare which
version they can apply.

## Failure modes the adapter must handle

| Case | Expected behaviour |
|---|---|
| `action` unknown to the adapter | reject, report `unsupported`, do not silently skip |
| parameter outside the caps | reject (the server should never send it, but verify) |
| `send_back` present | do not process; surface the reason to the user |
| `dry_run: true` | return the plan it would apply, change nothing |
