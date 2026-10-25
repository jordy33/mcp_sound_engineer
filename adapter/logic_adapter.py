"""Translate a DAW-neutral recipe into mcp_logic tool calls.

The adapter is deliberately honest about what it cannot do: Logic's mixer takes a
normalized 0..1 fader, plugin parameters are raw AX values by index, and limiter /
de-esser / dither are not insertable through the bridge. Every action ends up as
`ready`, `unresolved`, `needs_calibration`, `partial` or `unsupported`, and the
report says why.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# Default conversion, to be replaced by a calibration run per session.
# Logic's AX fader is 0..1 normalized; 1.0 = unity for a track fader is an
# assumption until calibrated. Only attenuations survive the clamp.
FADER_UNITY_AT_ZERO_DB = 1.0

CAPABILITIES = {
    "fader":          "ready",
    "pan":            "ready",
    "compressor":     "partial",           # insertable; only `threshold` (0..100) is writable
    "eq.high_pass":   "needs_calibration", # band/param index and raw AX value are unknown
    "eq.bell":        "needs_calibration",
    "eq.shelf":       "needs_calibration",
    "stereo.width":   "unsupported",
    "de_esser":       "unsupported",
    "limiter":        "unsupported",
    "dither":         "unsupported",
    "dynamic_eq":     "unsupported",
    "send_back":      "terminal",
}

MANUAL_HINT = {
    "limiter": "add the Limiter plug-in by hand and set the ceiling from the recipe",
    "de_esser": "insert a De-Esser and set 6.5 kHz / 2:1",
    "dither": "set the bounce bit depth; Logic dithers on export",
    "stereo.width": "insert a Stereo Spread / Direction Mixer and mono below 120 Hz",
    "dynamic_eq": "insert a multipressor or dynamic EQ by hand",
}


def db_to_fader(db: float) -> float:
    """Amplitude ratio, clamped to the 0..1 range the bridge accepts."""
    return round(max(0.0, min(1.0, FADER_UNITY_AT_ZERO_DB * 10 ** (float(db) / 20))), 4)


def plan(recipe: list[dict], tracks: dict[str, int] | None = None,
         master_track: int | None = None) -> list[dict]:
    """Recipe -> ordered steps. No network, no session changes."""
    tracks = tracks or {}
    steps: list[dict] = []
    for action in recipe:
        name = action.get("action")
        capability = CAPABILITIES.get(name, "unsupported")
        step = {"action": name, "target": action.get("target"), "status": capability}

        if capability == "terminal":
            step["reasons"] = action.get("reasons", [])
            steps.append(step)
            continue

        if capability == "unsupported":
            step["why"] = MANUAL_HINT.get(name, "no mcp_logic tool can apply this")
            steps.append(step)
            continue

        if capability == "needs_calibration":
            step["why"] = ("Logic exposes plugin parameters as raw AX values by index; "
                           "run the calibration sweep to map frequency/gain/Q")
            steps.append(step)
            continue

        track = _resolve_track(action, tracks, master_track)
        if track is None:
            step["status"] = "unresolved"
            step["why"] = (f"no track index for target {action.get('target')!r}; "
                           f"pass --tracks <role>=<index>")
            steps.append(step)
            continue

        if name == "fader":
            step["tool"] = "logic_mixer"
            step["arguments"] = {"command": "set_volume",
                                 "params": {"track": track,
                                            "value": db_to_fader(action.get("gain_db", 0.0))}}
        elif name == "pan":
            step["tool"] = "logic_mixer"
            step["arguments"] = {"command": "set_pan",
                                 "params": {"track": track,
                                            "value": float(action.get("position", 0.0))}}
        elif name == "compressor":
            step["tool"] = "logic_mixer"
            step["arguments"] = {"command": "insert_plugin",
                                 "params": {"track": track, "slot": 0,
                                            "plugin_name": "Compressor", "confirmed": True}}
            step["then"] = ("threshold is 0..100 and ratio/attack/release are not writable "
                            "through the bridge; set them by hand")
        steps.append(step)
    return steps


def _resolve_track(action: dict, tracks: dict[str, int], master_track: int | None) -> int | None:
    target = (action.get("target") or "master").lower()
    if target in ("master", "mix_bus", "bus"):
        return master_track
    return tracks.get(target)


def summary(steps: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"total": len(steps), "by_status": counts,
            "ready": sum(1 for s in steps if s["status"] in ("ready", "partial"))}


def apply(steps: list[dict], endpoint: str = "http://127.0.0.1:8765/mcp", timeout: int = 30) -> list[dict]:
    """Execute only the steps that carry a tool call, reporting every outcome."""
    results = []
    for index, step in enumerate(steps):
        if "tool" not in step:
            results.append({"index": index, "action": step["action"],
                            "status": step["status"], "skipped": True})
            continue
        body = {"jsonrpc": "2.0", "id": index + 1, "method": "tools/call",
                "params": {"name": step["tool"], "arguments": step["arguments"]}}
        request = urllib.request.Request(
            endpoint, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            result = payload.get("result", {})
            results.append({"index": index, "action": step["action"],
                            "status": "error" if result.get("isError") else "applied",
                            "detail": result.get("content", [{}])[0].get("text", "")[:200]})
        except (urllib.error.URLError, TimeoutError) as exc:
            results.append({"index": index, "action": step["action"],
                            "status": "error", "detail": str(exc)[:200]})
    return results
