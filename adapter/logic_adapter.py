"""Translate a DAW-neutral recipe into mcp_logic tool calls.

Grounded in the mcp_logic source (v3.18.0):

* `logic_mixer.set_volume/set_pan` take a raw 0..1 / -1..1 value.
* `logic_plugins.set_eq_band_verified` writes Channel EQ in **engineering units**
  (`unit: Hz|dB|Q`) and verifies against the host's AXValueDescription — no raw
  AX mapping is needed. Bands: Low Cut, Low Shelf, Peak 1..4, High Shelf, High Cut.
* `logic_plugins.set_param_verified` writes verified parameters; `unit` is optional
  (it comes from the parameter metadata). Compressor: threshold(%, normalized),
  ratio, attack, release, makeup. Limiter: gain, outputCeiling, release.
  Gain: gain, phaseInvert.
* Every verified write requires `mode: duplicate_applyback` and
  `project_expected_path`; `insert_verified` also requires `expected_name`.

Not supported by the bridge: de-esser and dither.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MODE = "duplicate_applyback"

CAPABILITIES = {
    "fader":             "ready",
    "pan":               "ready",
    "eq.high_pass":      "ready",      # frequency; slope is a host popup, left manual
    "eq.bell":           "ready",      # Hz + dB + Q
    "eq.shelf":          "ready",
    "eq.high_pass_stem": "ready",
    "compressor":        "ready",      # insert + ratio/attack/release
    "limiter":           "ready",      # insert Limiter + outputCeiling
    "polarity.invert":   "ready",      # Gain plug-in, phaseInvert
    "de_esser":          "unsupported",
    "dither":            "unsupported",
    "stereo.width":      "unsupported",
    "dynamic_eq":        "unsupported",
    "send_back":         "terminal",
}

MANUAL_HINT = {
    "de_esser": "no De-Esser in the verified catalog; insert it and set ~6.5 kHz by hand",
    "dither": "dither is a bounce setting, not a plug-in insert",
    "stereo.width": "no verified stereo-imaging parameter; use Stereo Spread by hand",
    "dynamic_eq": "no verified dynamic-EQ surface",
}

BAND_FOR_ROLE = {
    "high_pass": "Low Cut",
    "shelf_low": "Low Shelf",
    "shelf_high": "High Shelf",
    "bell": "Peak 2",
}

EQ_PARAMETER = {"freq": ("Frequency", "Hz"), "gain": ("Gain", "dB"), "q": ("Q-Factor", "Q")}


def db_to_fader(db: float) -> float:
    """Amplitude ratio clamped to the 0..1 the mixer accepts (attenuation only)."""
    return round(max(0.0, min(1.0, 10 ** (float(db) / 20))), 4)


def plan(recipe: list[dict], tracks: dict[str, dict] | None = None,
         master: dict | None = None, project_path: str | None = None) -> list[dict]:
    """Recipe -> ordered steps with explicit calls. No network, no session changes."""
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

        target = _resolve_target(action, tracks, master)
        if target is None:
            step["status"] = "unresolved"
            step["why"] = (f"no track for target {action.get('target')!r}; "
                           f"pass --tracks <role>=<index>:<Track Name>")
            steps.append(step)
            continue

        if name in ("fader", "pan"):
            steps.append(_mixer_step(step, name, action, target))
            continue

        if not project_path:
            step["status"] = "unresolved"
            step["why"] = "verified plugin writes need --project <path.logicx>"
            steps.append(step)
            continue

        if name in ("eq.bell", "eq.shelf", "eq.high_pass", "eq.high_pass_stem"):
            step["calls"] = _eq_calls(name, action, target, project_path)
            if name == "eq.high_pass":
                step["note"] = "slope is a host popup (host_choice_text); set it by hand"
        elif name == "compressor":
            step["calls"] = _compressor_calls(action, target, project_path)
        elif name == "limiter":
            step["calls"] = _limiter_calls(action, target, project_path)
        elif name == "polarity.invert":
            step["calls"] = _polarity_calls(target, project_path)
        steps.append(step)
    return steps


# --------------------------------------------------------------------------
# call builders
# --------------------------------------------------------------------------
def _resolve_target(action: dict, tracks: dict, master: dict | None) -> dict | None:
    target = (action.get("target") or "master").lower()
    if target in ("master", "mix_bus", "bus"):
        return master
    if target == "stem":
        stem = action.get("stem")
        if not stem:
            return None
        base = os.path.basename(stem)
        return tracks.get(base) or tracks.get(os.path.splitext(base)[0])
    return tracks.get(target)


def _mixer_step(step: dict, name: str, action: dict, target: dict) -> dict:
    if name == "fader":
        command, params = "set_volume", {"track": target["index"],
                                         "value": db_to_fader(action.get("gain_db", 0.0))}
    else:
        command, params = "set_pan", {"track": target["index"],
                                      "value": float(action.get("position", 0.0))}
    step["calls"] = [{"tool": "logic_mixer",
                      "arguments": {"command": command, "params": params}}]
    return step


def _eq_calls(name: str, action: dict, target: dict, project_path: str) -> list[dict]:
    band = BAND_FOR_ROLE["high_pass"] if name == "eq.high_pass" else action.get("band_name")
    if name == "eq.high_pass_stem":
        band = BAND_FOR_ROLE["high_pass"]
    band = band or (BAND_FOR_ROLE["shelf_high"] if name == "eq.shelf" else BAND_FOR_ROLE["bell"])
    wanted = []
    if "freq_hz" in action:
        wanted.append(("freq", float(action["freq_hz"])))
    if name in ("eq.bell", "eq.shelf") and "gain_db" in action:
        wanted.append(("gain", float(action["gain_db"]) if name != "eq.shelf"
                       else abs(float(action["gain_db"]))))
    if name == "eq.bell" and "q" in action:
        wanted.append(("q", float(action["q"])))
    calls = []
    for key, value in wanted:
        parameter, unit = EQ_PARAMETER[key]
        calls.append({"tool": "logic_plugins",
                      "arguments": {"command": "set_eq_band_verified",
                                    "params": {"track": target["index"],
                                               "insert": action.get("insert", 0),
                                               "band": band, "parameter": parameter,
                                               "value": value, "unit": unit,
                                               "mode": MODE,
                                               "project_expected_path": project_path}}})
    return calls


def _insert_call(target: dict, plugin: str, project_path: str) -> dict:
    return {"tool": "logic_plugins",
            "arguments": {"command": "insert_verified",
                          "params": {"track": target["index"], "insert": 0,
                                     "plugin_name": plugin, "mode": MODE,
                                     "project_expected_path": project_path,
                                     "expected_name": target.get("name", "")}}}


def _param_call(target: dict, plugin: str, param: str, value: float,
                project_path: str, unit: str | None = None) -> dict:
    params = {"track": target["index"], "insert": 0, "plugin": plugin, "param": param,
              "value": value, "mode": MODE, "project_expected_path": project_path}
    if unit:
        params["unit"] = unit
    return {"tool": "logic_plugins",
            "arguments": {"command": "set_param_verified", "params": params}}


def _compressor_calls(action: dict, target: dict, project_path: str) -> list[dict]:
    calls = [_insert_call(target, "Compressor", project_path)]
    for key, param in (("ratio", "ratio"), ("attack_ms", "attack"), ("release_ms", "release")):
        if key in action:
            calls.append(_param_call(target, "Compressor", param, float(action[key]),
                                     project_path))
    # threshold is normalized percent in the verified catalog, not dB: left manual
    return calls


def _limiter_calls(action: dict, target: dict, project_path: str) -> list[dict]:
    calls = [_insert_call(target, "Limiter", project_path)]
    if "ceiling_dbtp" in action:
        calls.append(_param_call(target, "Limiter", "outputCeiling",
                                 float(action["ceiling_dbtp"]), project_path, unit="dB"))
    return calls


def _polarity_calls(target: dict, project_path: str) -> list[dict]:
    return [_insert_call(target, "Gain", project_path),
            _param_call(target, "Gain", "phaseInvert", 1, project_path, unit="boolean")]


# --------------------------------------------------------------------------
# reporting / execution
# --------------------------------------------------------------------------
def summary(steps: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    return {"total": len(steps), "by_status": counts,
            "calls": sum(len(s.get("calls", [])) for s in steps)}


def apply(steps: list[dict], endpoint: str = "http://127.0.0.1:8765/mcp",
          timeout: int = 60) -> list[dict]:
    results: list[dict] = []
    for index, step in enumerate(steps):
        if not step.get("calls"):
            results.append({"index": index, "action": step["action"],
                            "status": step["status"], "skipped": True})
            continue
        for n, call in enumerate(step["calls"]):
            results.append(_post(index, step, call, n, endpoint, timeout))
    return results


def _post(index: int, step: dict, call: dict, n: int, endpoint: str, timeout: int) -> dict:
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
                "detail": result.get("content", [{}])[0].get("text", "")[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"index": index, "action": step["action"], "call": n,
                "status": "error", "detail": str(exc)[:200]}
