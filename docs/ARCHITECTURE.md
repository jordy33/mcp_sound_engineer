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

The adapter in this repo is a client, not a server: it consumes the recipe and speaks JSON-RPC to
`mcp_logic`. The closed loop is a second entry point over the same pieces:

```
recipe ──► plan (adapt) ──► apply State A ──► export_plan/export_run ──► analyze_master
              ▲                   │                                          │
              │                   └──────────────┐                           ▼
              │                                  ▼                 compare_spectra
              └── correction (track EQ only) ◄── decide ◄────────  (FWHM window, residual)
```

The measured side is read-only. The only feedback path is a verified Channel EQ gain correction;
faders, pan and shelves are applied once, and the master EQ is never corrected.

## Why not fuse them

* **Portability.** The judgement layer is DAW-agnostic; adapters are disposable.
* **Trust boundary.** Measuring is deterministic and cheap; deciding calls a network model.
  Keeping them apart lets you audit, cache and rate-limit independently.
* **Surface area.** An adapter needs one contract (the recipe schema). If they were fused, every
  new DAW would re-implement the measurement and the rules.

## Contract

The only coupling is `docs/RECIPE.md` (schema `2.1.0`). It is versioned; adapters declare which
version they can apply.

## Failure modes the adapter must handle

| Case | Expected behaviour |
|---|---|
| `action` unknown to the adapter | reject, report `unsupported`, do not silently skip |
| parameter outside the caps | reject (the server should never send it, but verify) |
| `send_back` present | do not process; surface the reason to the user |
| `dry_run: true` | return the plan it would apply, change nothing |
| adapter action outside the verified surface | plan it as `manual`, emit no call, state the reason |
| JSON-RPC error envelope / missing Honest Contract fields | never report `applied`; stop |
| write accepted by transport but State B/C | stop the run; report `reason`/`error` and any `write_attempted`; no retry, no rollback |
| apply partially completes (State C after earlier writes) | report `partial` with every result and the possible mutation; do not continue the chain |
