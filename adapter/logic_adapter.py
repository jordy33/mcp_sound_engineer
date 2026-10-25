"""Translate a DAW-neutral recipe into mcp_logic tool calls.

The adapter is deliberately honest about what it cannot do: Logic's mixer takes a
normalized 0..1 fader, plugin parameters are raw AX values by index, and limiter /
de-esser / dither are not insertable through the bridge. Every action ends up as
`ready`, `unresolved`, `needs_calibration`, `partial` or `unsupported`, and the
report says why.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .calibration import Calibration

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
    "polarity.invert": "unsupported",
    "eq.high_pass_stem": "needs_calibration",
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
    "polarity.invert": "no bridge tool for polarity; flip the track's phase by hand",
}


def db_to_fader(db: float) -> float:
    """Amplitude ratio, clamped to the 0..1 range the bridge accepts."""
    return round(max(0.0, min(1.0, FADER_UNITY_AT_ZERO_DB * 10 ** (float(db) / 20))), 4)


def plan(recipe: list[dict], tracks: dict[str, int] | None = None,
         master_track: int | None = None,
         calibration: Calibration | None = None) -> list[dict]:
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
            calls = _eq_calls(action, tracks, master_track, calibration)
            if calls:
                step["status"] = "ready"
                step["calls"] = calls
            else:
                step["why"] = ("Logic exposes plugin parameters as raw AX values by index; "
                               "load a calibration table (see calibration.example.json)")
            steps.append(step)
            continue

        track = _resolve_track(action, tracks, master_track)
        if track is None:
            step["status"] = "unresolved"
            step["why"] = (f"no track index for target {action.get('target')!r}; "
                           f"pass --tracks <role>=<index> (or <file>.wav=<index> for stems)")
            steps.append(step)
            continue

        if name == "fader":
            step["calls"] = [{"tool": "logic_mixer",
                              "arguments": {"command": "set_volume",
                                            "params": {"track": track,
                                                       "value": db_to_fader(action.get("gain_db", 0.0))}}}]
        elif name == "pan":
            step["calls"] = [{"tool": "logic_mixer",
                              "arguments": {"command": "set_pan",
                                            "params": {"track": track,
                                                       "value": float(action.get("position", 0.0))}}}]
        elif name == "compressor":
            step["calls"] = [{"tool": "logic_mixer",
                              "arguments": {"command": "insert_plugin",
                                            "params": {"track": track, "slot": 0,
                                                       "plugin_name": "Compressor",
                                                       "confirmed": True}}}]
            step["then"] = ("threshold is 0..100 and ratio/attack/release are not writable "
                            "through the bridge; set them by hand")
        steps.append(step)
    return steps


def _eq_calls(action: dict, tracks: dict, master_track: int | None,
              calibration: Calibration | None) -> list[dict]:
    """A calibrated EQ action becomes insert + one set_plugin_param per parameter."""
    if calibration is None or not calibration.covers(["freq", "gain", "q"]):
        return []
    track = _resolve_track(action, tracks, master_track)
    if track is None:
        return []
    freq = float(action.get("freq_hz", 1000.0))
    gain = float(action.get("gain_db", 0.0))
    q = float(action.get("q", 1.0))
    calls = [{"tool": "logic_mixer",
              "arguments": {"command": "insert_plugin",
                            "params": {"track": track, "slot": 0,
                                       "plugin_name": calibration.plugin.get("plugin_name",
                                                                             "Channel EQ"),
                                       "confirmed": True}}}]
    for name, value in (("freq", freq), ("gain", gain), ("q", q)):
        raw = calibration.to_raw(name, value)
        calls.append({"tool": "logic_mixer",
                      "arguments": {"command": "set_plugin_param",
                                    "params": {"track": track, "insert": 0,
                                               "param": calibration.param_index(name),
                                               "value": raw}}})
    return calls


def _resolve_track(action: dict, tracks: dict[str, int], master_track: int | None) -> int | None:
    target = (action.get("target") or "master").lower()
    if target in ("master", "mix_bus", "bus"):
        return master_track
    if target == "stem":
        stem = action.get("stem")
        if not stem:
            return None
        base = os.path.basename(stem)
        return tracks.get(base) or tracks.get(os.path.splitext(base)[0]) or tracks.get(stem)
    return tracks.get(target)


def summary(steps: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"total": len(steps), "by_status": counts,
            "ready": sum(1 for s in steps if s["status"] in ("ready", "partial"))}


def apply(steps: list[dict], endpoint: str = "http://127.0.0.1:8765/mcp",
          timeout: int = 30) -> list[dict]:
    """Execute only the steps that carry calls, reporting every outcome."""
    results: list[dict] = []
    for index, step in enumerate(steps):
        if not step.get("calls"):
            results.append({"index": index, "action": step["action"],
                            "status": step["status"], "skipped": True})
            continue
        for n, call in enumerate(step["calls"]):
            results.append(_post(index, step, call, n, endpoint, timeout))
    return results


def _post(index: int, step: dict, call: dict, n: int,
          endpoint: str, timeout: int) -> dict:
    body = {"jsonrpc": "2.0", "id": index * 100 + n, "method": "tools/call",
            "params": {"name": call["tool"], "arguments": call["arguments"]}}
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
        result = payload.get("result", {})
        return {"index": index, "action": step["action"], "call": n,
                "status": "error" if result.get("isError") else "applied",
                "detail": result.get("content", [{}])[0].get("text", "")[:200]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"index": index, "action": step["action"], "call": n,
                "status": "error", "detail": str(exc)[:200]}
