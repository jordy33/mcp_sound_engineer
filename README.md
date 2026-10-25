# mcp_sound_engineer

The **ears and the judgement** of an AI sound engineer, as an [MCP](https://modelcontextprotocol.io) server.

It measures audio, decides like a mix/master engineer, and returns an **executable recipe** that
[`mcp_logic`](https://github.com/jordy33/mcp_logic) applies inside Logic Pro.

```
        ┌──────────────────────────┐        ┌─────────────────────┐        ┌──────────────┐
 audio  │  mcp_sound_engineer      │ typed  │  agent / LLM        │  tool  │  mcp_logic   │
───────►│  measure → judge → recipe│───────►│  (plans the session)│───────►│  (Logic Pro) │
        └──────────────────────────┘        └─────────────────────┘        └──────────────┘
              numpy + rules                    Jev (typed decisions)
```

Two hats, one server:

* **Mixing engineer** — takes the recorded tracks (vocals, instruments, drums), balances levels,
  cleans imperfections, applies EQ/compression/reverb so they coexist.
* **Mastering engineer** — final step: polishes and unifies the sound for streaming, radio or cinema.

## Why typed decisions

The judge is [Jev](https://jevtypesafeai.com), a *System One* decision model: it does not write prose,
it returns `choice` / `score` / `noul` answers with calibrated probabilities. So the output of this
server is machine-actionable — no parsing, no hallucinated plugin names, no "sounds a bit muddy".

The professional rules live in [`server/rules.py`](server/rules.py) and are enforced as hard caps:
a decision can never exceed the limits of the craft (>3 dB corrective EQ, 3 dB gain reduction, etc.).

## Tools

| Tool | Input | Returns |
|---|---|---|
| `analyze_master` | one stereo file | LUFS, true peak, LRA, crest, phase correlation, 6 bands, clipping |
| `analyze_stems` | N files | per-stem loudness + bands, pairwise masking matrix |
| `judge_master` | path (+ target platform) | `answers` (typed) + `recipe` for Logic |
| `judge_mix` | stem paths | `answers` + `recipe` (faders, EQ, bus compression) |
| `engineer_info` | — | rules version, model, configured targets |

## The rules

Sourced from professional mastering methodology
([mastering-engineer skill](https://github.com/fadelabs/phantom/blob/main/plugin/skills/mastering-engineer/SKILL.md),
[mastering guide 2026](https://www.masteringthemix.com/pages/audio-mastering-guide)):

| Gate | Threshold |
|---|---|
| Send back: corrective EQ at one point | > 3 dB |
| Send back: cumulative corrective EQ | > 6 dB |
| Send back: phase correlation | < +0.3 |
| Send back: noise floor | > −50 dB (moderate) |
| Send back: lossy source | drop > 20 dB above the HF shelf |
| True peak ceiling | −1.0 dBTP |
| Broadband compression | 1.5–3:1, attack 10–30 ms, GR ≤ 3 dB |
| Corrective EQ Q | 4–8 |
| Loudness targets | Spotify −14, Apple Music −16, YouTube −14, CD −9…−14 |

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your JEV_API_KEY
```

## Run

```bash
python server/engineer_server.py        # newline-delimited JSON-RPC 2.0 on stdin/stdout
```

Register next to `mcp_logic`:

```json
{
  "mcpServers": {
    "engineer": { "command": "python", "args": ["/abs/path/mcp_sound_engineer/server/engineer_server.py"] },
    "logic":    { "command": "mcp-logic", "args": [] }
  }
}
```

## Example

```json
{"tool": "judge_master", "arguments": {"path": "/renders/take-3.wav", "platform": "spotify"}}
```

```json
{
  "facts":  {"loudness": {"integrated_lufs": -11.2, "true_peak_dbtp": 0.4}, "spectral": {...}},
  "answers": {
    "send_back": {"type": "noul", "noul": 0.12},
    "low_end":   {"type": "score", "score": 3.4, "confidence": 0.71},
    "limiter":   {"type": "score", "score": 1.2, "confidence": 0.66}
  },
  "recipe": [
    {"plugin": "Channel EQ", "type": "high_pass", "freq_hz": 25, "slope_db_oct": 12},
    {"plugin": "Channel EQ", "type": "bell_cut", "freq_hz": 300, "gain_db": -2.0, "q": 5.0},
    {"plugin": "Limiter", "ceiling_dbtp": -1.0, "target_lufs": -14.0}
  ]
}
```

## Status

Early. The measurement layer and the rules are real; the Jev schemas are the first version.
`mcp_logic` exposes the execution side.
