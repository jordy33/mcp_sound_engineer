#!/usr/bin/env python3
"""mcp_sound_engineer — MCP server (newline-delimited JSON-RPC 2.0 over stdio).

Measures audio, asks Jev for typed decisions, returns an executable recipe
for mcp_logic to apply inside Logic Pro.
"""
from __future__ import annotations

import json
import sys

from analysis import analyze_master, analyze_stems
from jev import JevError, ask, config
import rules

SERVER_NAME = "mcp_sound_engineer"
SERVER_VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Jev schemas
# --------------------------------------------------------------------------
MASTER_QUESTIONS = {
    "send_back": {"type": "noul",
                  "instructions": "Does this mix need more than 3 dB of corrective EQ, or does it "
                                  "have phase/clipping/lossy-source problems that mastering cannot fix?"},
    "low_end": {"type": "score",
                "instructions": "Low end vs reference_curve: how much low-mid/sub excess is there?",
                "criteria": ["well balanced", "slight excess", "noticeable", "heavy", "extreme"]},
    "high_mid": {"type": "score",
                 "instructions": "2-6 kHz vs reference_curve: how much presence excess is there?",
                 "criteria": ["none", "slight", "noticeable", "heavy", "extreme"]},
    "sibilance": {"type": "choice",
                  "instructions": "Which sibilance treatment fits, given the 5-9 kHz balance?",
                  "criteria": {"none": "no treatment needed",
                               "deess": "dynamic de-esser",
                               "cut": "narrow static cut around 7 kHz"}},
    "dynamics": {"type": "choice",
                 "instructions": "Dynamics work needed? LRA < 4 LU or crest < 6 dB means already compressed.",
                 "criteria": {"none": "already dense, do not compress",
                              "glue": "gentle bus glue, 1-3 dB GR",
                              "expand": "upward expansion to restore range"}},
    "limiter": {"type": "score",
                "instructions": "How much limiter gain reduction is appropriate to reach the target LUFS?",
                "criteria": ["0-1 dB", "1-2 dB", "2-3 dB", "3-4 dB", "more than 4 dB"]},
    "dither": {"type": "choice",
               "instructions": "Output bit depth for this delivery",
               "criteria": {"none_24": "24-bit, no dither", "tpdf_16": "16-bit with TPDF dither"}},
}

MIX_QUESTIONS = {
    "balance": {"type": "choice",
                "instructions": "Which element is out of balance in the stems?",
                "criteria": {"balanced": "nothing is out of place",
                             "vocal_forward": "vocal sits too high",
                             "drums_forward": "drums dominate",
                             "bass_heavy": "bass masks everything"}},
    "masking": {"type": "score",
                "instructions": "How much spectral masking is there between the stems?",
                "criteria": ["none", "slight", "noticeable", "heavy", "extreme"]},
    "bus_comp": {"type": "choice",
                 "instructions": "Does the mix bus need glue compression?",
                 "criteria": {"none": "no compression", "glue": "gentle bus glue"}},
}


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def tool_analyze_master(path: str) -> dict:
    return analyze_master(path)


def tool_analyze_stems(paths: list[str]) -> dict:
    return analyze_stems(paths)


def tool_judge_master(path: str, platform: str = "spotify") -> dict:
    facts = analyze_master(path)
    target = rules.loudness_target(platform)
    state = {
        "context": {"stage": "mastering", "platform": platform,
                    "target_lufs": target["lufs"], "target_true_peak": target["true_peak"]},
        "facts": facts,
        "reference_curve": rules.REFERENCE_CURVE,
        "limits": rules.GATE,
    }
    answers = ask(state, MASTER_QUESTIONS)
    return {"facts": facts, "answers": answers,
            "recipe": rules.build_master_recipe(answers, facts, platform),
            "target": target}


def tool_judge_mix(paths: list[str]) -> dict:
    facts = analyze_stems(paths)
    state = {"context": {"stage": "mixing"}, "facts": facts, "limits": rules.GATE}
    answers = ask(state, MIX_QUESTIONS)
    return {"facts": facts, "answers": answers,
            "recipe": rules.build_mix_recipe(answers, [s["name"] for s in facts["stems"]])}


def tool_engineer_info() -> dict:
    cfg = config()
    return {"server": SERVER_NAME, "version": SERVER_VERSION,
            "rules_version": rules.RULES_VERSION,
            "jev_configured": bool(cfg["key"]), "jev_model": cfg["model"],
            "platforms": sorted(rules.PLATFORMS),
            "tools": ["analyze_master", "analyze_stems", "judge_master", "judge_mix", "engineer_info"]}


TOOLS = {
    "analyze_master": {
        "description": "Measure one stereo file: LUFS, true peak, LRA, crest, phase correlation, "
                       "6-band balance, clipping. Deterministic, offline.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"]},
        "handler": lambda a: tool_analyze_master(a["path"]),
    },
    "analyze_stems": {
        "description": "Measure N stems: per-stem loudness and band balance, plus pairwise masking.",
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]},
        "handler": lambda a: tool_analyze_stems(a["paths"]),
    },
    "judge_master": {
        "description": "Typed mastering decisions via Jev plus an executable Logic Pro recipe. "
                       "Enforces the craft's hard caps (3 dB corrective, 3 dB GR, -1 dBTP).",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "platform": {"type": "string", "enum": list(rules.PLATFORMS)}}, "required": ["path"]},
        "handler": lambda a: tool_judge_master(a["path"], a.get("platform", "spotify")),
    },
    "judge_mix": {
        "description": "Typed mixing decisions via Jev: balance, masking, bus compression, "
                       "and the resulting fader/EQ moves.",
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]},
        "handler": lambda a: tool_judge_mix(a["paths"]),
    },
    "engineer_info": {
        "description": "Server status: rules version, Jev configuration, supported platforms.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda a: tool_engineer_info(),
    },
}


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------
def make_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        return make_result(rid, {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return make_result(rid, {})
    if method == "tools/list":
        return make_result(rid, {"tools": [
            {"name": name, "description": t["description"], "inputSchema": t["inputSchema"]}
            for name, t in TOOLS.items()]})
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        tool = TOOLS.get(name)
        if not tool:
            return make_error(rid, -32602, f"unknown tool: {name}")
        try:
            payload = tool["handler"](args)
            text = json.dumps(payload, ensure_ascii=False, indent=1)
            return make_result(rid, {"content": [{"type": "text", "text": text}],
                                     "isError": False})
        except (JevError, FileNotFoundError, ValueError, RuntimeError) as exc:
            return make_result(rid, {"content": [{"type": "text", "text": f"error: {exc}"}],
                                     "isError": True})
    return make_error(rid, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(make_error(None, -32700, "parse error")), flush=True)
            continue
        reply = handle(msg)
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
