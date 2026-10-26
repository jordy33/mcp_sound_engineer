#!/usr/bin/env python3
"""mcp_sound_engineer — MCP server (newline-delimited JSON-RPC 2.0 over stdio).

Measures audio, asks a decision model for typed answers, and returns an
executable, DAW-neutral recipe. Applying it is another server's job.
"""
from __future__ import annotations

import json
import sys

try:  # installed as a package
    from . import rules
    from .analysis import analyze_master, analyze_reference, analyze_sections, analyze_stems
    from .jev import ask, config
except ImportError:  # executed as a script: python server/engineer_server.py
    import rules
    from analysis import analyze_master, analyze_reference, analyze_sections, analyze_stems
    from jev import ask, config

SERVER_NAME = "mcp_sound_engineer"
SERVER_VERSION = "0.6.0"

# Jev schemas live in rules.py: one source of truth for the answer keys.
MASTER_QUESTIONS = rules.MASTER_QUESTIONS
MIX_QUESTIONS = rules.MIX_QUESTIONS


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def tool_analyze_master(path: str) -> dict:
    return analyze_master(path)


def tool_analyze_stems(paths: list[str]) -> dict:
    return analyze_stems(paths)


def tool_analyze_sections(path: str, window_s: float = 30.0) -> dict:
    return analyze_sections(path, window_s)


def tool_analyze_reference(path: str) -> dict:
    return analyze_reference(path)


def tool_validate_recipe(recipe: list) -> dict:
    violations = rules.validate_recipe(recipe)
    return {"valid": not violations, "violations": violations,
            "schema": rules.RECIPE_SCHEMA_VERSION}


def tool_judge_master(path: str, platform: str = "spotify",
                      reference_path: str | None = None,
                      previous_path: str | None = None,
                      desired_lufs: float | None = None) -> dict:
    facts = analyze_master(path)
    target = rules.loudness_target(platform)
    if reference_path:
        measured = analyze_reference(reference_path)
        reference_curve = measured["reference_curve"]
    else:
        measured, reference_curve = None, rules.REFERENCE_CURVE
    gates = rules.evaluate_gates(facts)
    warnings = rules.loudness_warnings(facts, target)
    if gates["send_back"]:
        # Measured refusals must not depend on a network call or a key.
        recipe = [rules._action("send_back", reasons=gates["reasons"])]
        warnings += rules.limiter_warnings(recipe)
        return {"facts": facts, "answers": {}, "gates": gates,
                "recipe": recipe, "target": target, "reference": measured,
                "reference_curve": reference_curve, "warnings": warnings}

    state = {
        "context": {"stage": "mastering", "platform": platform,
                    "playback_normalization_lufs": target["normalization_lufs"],
                    "delivery_true_peak_ceiling": target["true_peak_ceiling"],
                    "note": "streaming loudness is normalized on playback; the master is not "
                            "required to reach the normalization figure"},
        "facts": facts,
        "reference_curve": reference_curve,
        "limits": rules.GATE,
    }
    if measured is not None:
        state["reference_sibilance_db_rel"] = measured.get("sibilance_db_rel")
    answers = ask(state, MASTER_QUESTIONS)
    confidence = rules.confidence_report(answers)
    recipe = rules.build_master_recipe(answers, facts, platform,
                                       reference_curve=reference_curve,
                                       desired_lufs=desired_lufs)
    warnings += rules.limiter_warnings(recipe)
    if confidence["needs_human"]:
        warnings.append(f"low confidence on {sorted(confidence['low'])}; treat the recipe as a draft")
    if previous_path:
        drop = rules.correlation_drop(analyze_master(previous_path), facts)
        if drop["exceeds"]:
            warnings.append(f"phase correlation dropped {drop['drop']} after the chain "
                            f"({drop['before']} -> {drop['after']}); check the last stage added")
    return {"facts": facts, "answers": answers, "recipe": recipe,
            "target": target,
            "reference": measured,
            "reference_curve": reference_curve,
            "confidence": confidence,
            "warnings": warnings}


def tool_judge_mix(paths: list[str]) -> dict:
    facts = analyze_stems(paths)
    state = {"context": {"stage": "mixing"}, "facts": facts, "limits": rules.GATE}
    answers = ask(state, MIX_QUESTIONS)
    names = [s["path"] for s in facts["stems"]]
    confidence = rules.confidence_report(answers)
    warnings = rules.mix_warnings(answers, names)
    if confidence["needs_human"]:
        warnings.append(f"low confidence on {sorted(confidence['low'])}; treat the recipe as a draft")
    warnings += [f"{g['gate']}: {g}" for g in facts.get("gates", [])]
    return {"facts": facts, "answers": answers,
            "recipe": rules.build_mix_recipe(answers, names, facts["stems"]),
            "confidence": confidence,
            "warnings": warnings}


def tool_engineer_info() -> dict:
    cfg = config()
    return {"server": SERVER_NAME, "version": SERVER_VERSION,
            "rules_version": rules.RULES_VERSION,
            "jev_configured": bool(cfg["key"]), "jev_model": cfg["model"],
            "platforms": sorted(rules.PLATFORMS),
            "tools": sorted(TOOLS)}


TOOLS = {
    "analyze_master": {
        "description": "Measure one stereo file: LUFS, true peak, LRA, crest, phase correlation, "
                       "6-band balance, clipping. Deterministic, offline.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"]},
        "handler": lambda a: tool_analyze_master(a["path"]),
    },
    "analyze_sections": {
        "description": "Per-window analysis (default 30 s): LUFS, band balance and correlation "
                       "per section, plus which windows deviate from the file median.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "window_s": {"type": "number"}}, "required": ["path"]},
        "handler": lambda a: tool_analyze_sections(a["path"], a.get("window_s", 30.0)),
    },
    "analyze_reference": {
        "description": "Measure a commercial reference track; its band balance becomes the "
                       "reference curve used by judge_master.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"]},
        "handler": lambda a: tool_analyze_reference(a["path"]),
    },
    "analyze_stems": {
        "description": "Measure N stems: per-stem loudness and band balance, plus pairwise masking.",
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]},
        "handler": lambda a: tool_analyze_stems(a["paths"]),
    },
    "judge_master": {
        "description": "Typed mastering decisions plus an executable, DAW-neutral recipe. "
                       "Enforces the craft's hard caps (3 dB corrective, 3 dB GR, -1 dBTP).",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "platform": {"type": "string", "enum": list(rules.PLATFORMS)},
            "reference_path": {"type": "string"},
            "previous_path": {"type": "string"},
            "desired_lufs": {"type": "number",
                             "description": "Optional loudness request for the limiter input "
                                            "gain; omit for peak-only (recommended). Not a "
                                            "delivery target."}}, "required": ["path"]},
        "handler": lambda a: tool_judge_master(a["path"], a.get("platform", "spotify"),
                                               a.get("reference_path"), a.get("previous_path"),
                                               a.get("desired_lufs")),
    },
    "judge_mix": {
        "description": "Typed mixing decisions via Jev: balance, masking, bus compression, "
                       "and the resulting fader/EQ moves.",
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]},
        "handler": lambda a: tool_judge_mix(a["paths"]),
    },
    "validate_recipe": {
        "description": "Adapter contract check: verify a recipe against the schema and the caps "
                       "before applying it. Returns {valid, violations}.",
        "inputSchema": {"type": "object", "properties": {
            "recipe": {"type": "array", "items": {"type": "object"}}}, "required": ["recipe"]},
        "handler": lambda a: tool_validate_recipe(a["recipe"]),
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
    if not isinstance(msg, dict):
        return make_error(None, -32600, "invalid request: expected a JSON object")
    method = msg.get("method")
    rid = msg.get("id")
    if not isinstance(method, str):
        return make_error(rid, -32600, "invalid request: method must be a string")
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return make_error(rid, -32602, "invalid params: expected a JSON object")
    if method == "initialize":
        return make_result(rid, {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
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
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(name, str):
            return make_error(rid, -32602, "invalid params: name must be a string")
        if not isinstance(args, dict):
            return make_error(rid, -32602, "invalid params: arguments must be a JSON object")
        tool = TOOLS.get(name)
        if not tool:
            return make_error(rid, -32602, f"unknown tool: {name}")
        try:
            payload = tool["handler"](args)
            text = json.dumps(payload, ensure_ascii=False, indent=1)
            return make_result(rid, {"content": [{"type": "text", "text": text}],
                                     "isError": False})
        except Exception as exc:  # noqa: BLE001 — a bad file must not kill the server
            return make_result(rid, {"content": [{"type": "text", "text": f"error: {exc}"}],
                                     "isError": True})
    return make_error(rid, -32601, f"method not found: {method}")


def _dispatch(msg) -> dict | None:
    """Transport guard: no unexpected exception may kill the server loop."""
    try:
        return handle(msg)
    except Exception as exc:  # noqa: BLE001
        rid = msg.get("id") if isinstance(msg, dict) else None
        return make_error(rid, -32603, f"internal error: {exc}")


def serve_http(port: int = 8766) -> None:
    """Optional transport, mirroring the usual MCP bridge shape: POST /mcp + GET /health."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self._send(200, {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})
            else:
                self._send(200, {"server": SERVER_NAME, "mcp": "POST /mcp", "health": "/health"})

        def do_POST(self):  # noqa: N802
            if self.path not in ("/mcp", "/"):
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                msg = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, make_error(None, -32700, "parse error"))
            reply = _dispatch(msg)
            if reply is None:
                return self._send(202, {"status": "accepted"})
            self._send(200, reply)

        def log_message(self, *args):  # silence per-request logging
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        if getattr(exc, "errno", None) == 48:
            print(f"[engineer] port {port} is already in use.", flush=True)
            print("[engineer]   stop it:  ./start-sound-engineer-mcp.sh --stop", flush=True)
            print(f"[engineer]   or use:   ./start-sound-engineer-mcp.sh --port {port + 1}", flush=True)
            raise SystemExit(2)
        raise
    cfg = config()
    print(f"[engineer] listening on http://127.0.0.1:{port}", flush=True)
    print("[engineer]   MCP JSON-RPC : POST /mcp", flush=True)
    print("[engineer]   health       : GET /health", flush=True)
    print(f"[engineer]   rules        : {rules.RULES_VERSION}  schema {rules.RECIPE_SCHEMA_VERSION}", flush=True)
    print(f"[engineer]   tools        : {', '.join(TOOLS)}", flush=True)
    if cfg["key"]:
        print(f"[engineer]   jev          : configured ({cfg['model']})", flush=True)
    else:
        print("[engineer]   jev          : NOT configured — put JEV_API_KEY in .env "
              "(analyze_* still work)", flush=True)
    srv.serve_forever()


def main() -> None:
    if "--http" in sys.argv:
        port = 8766
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        serve_http(port)
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(make_error(None, -32700, "parse error")), flush=True)
            continue
        reply = _dispatch(msg)
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
