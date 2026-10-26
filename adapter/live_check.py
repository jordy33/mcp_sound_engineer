"""Manual live probe for Logic + mcp_logic.  NEVER run from tests or CI.

This module is deliberately outside the test runner and the CI workflow: it
talks to a real Logic Pro project through the mcp_logic bridge.  Run it only
with Logic open and a **disposable scratch project**:

    # read-only: health, track and output-strip inventory, export_plan
    python -m adapter.live_check --project /path/scratch.logicx \
        --track 'vocal=3:Lead Vocal'

    # explicit write: insert Channel EQ on a TRACK, write Peak 1, read it back
    python -m adapter.live_check --project /path/scratch.logicx \
        --track 'vocal=3:Lead Vocal' --write --confirm

The write path never targets the master strip: the output strip has no
``plugin_insert_ref`` and its Channel EQ state cannot be read back.  There is
no rollback.  A State C result with ``write_attempted: true`` may have left a
mutation in the project, and this tool says so instead of retrying.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import mcp_client
from . import mcp_logic_contract as contract
from .cli import _parse_master, _parse_targets
from .inventory import (
    fetch_chain,
    find_plugins,
    first_empty_slot,
    verify_insert,
)

DEFAULT_ENDPOINT = "http://127.0.0.1:8765/mcp"
SCRATCH_WARNING = (
    "live_check writes to a real Logic project. Use only a disposable scratch "
    "project; this tool has no rollback.")
PEAK_BAND = "Peak 1"
TEST_FREQ_HZ = 800.0
TEST_GAIN_DB = -1.0
TEST_Q = 1.0


class _Abort(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


def _call(client, tool: str, command: str, params: dict, timeout=None):
    arguments = {"command": command, "params": params}
    call = {"tool": tool, "arguments": arguments}
    try:
        contract.validate_call(call)
    except contract.ContractViolation as exc:
        raise _Abort("refusing to emit %s.%s: %s" % (tool, command, exc))
    return client.call_tool(tool, arguments, timeout=timeout)


def _result_record(result) -> dict:
    payload = result.payload if isinstance(result.payload, dict) else {}
    return {
        "ok": bool(result.ok),
        "state": result.state,
        "verified": result.verified,
        "success": result.success,
        "hc_schema": result.hc_schema,
        "reason": result.reason,
        "error": result.error,
        "write_attempted": bool(payload.get("write_attempted", False)),
        "payload": payload,
    }


def _require_state_a(result, what: str) -> dict:
    record = _result_record(result)
    if not result.state_a:
        mutated = record["write_attempted"]
        detail = ("%s did not reach State A (%s)." % (what, result.describe()))
        if mutated:
            detail += (" write_attempted=true: the mutation may have landed. "
                       "No rollback was attempted and this tool will not retry.")
        raise _Abort(detail)
    return record


def _state_band(payload: dict, band: str):
    for item in payload.get("bands") or []:
        if isinstance(item, dict) and item.get("name") == band:
            return item
    return None


def _read_eq_state(client, ref: str) -> dict:
    result = _call(client, contract.TOOL_PLUGINS,
                   "get_channel_eq_state_verified", {"target_ref": ref},
                   timeout=mcp_client.WRITE_TIMEOUT)
    record = _require_state_a(result, "channel EQ readback")
    payload = record["payload"]
    if payload.get("complete") is not True:
        raise _Abort("channel EQ state is incomplete: %s"
                     % (payload.get("partial_reasons") or "unknown"))
    return payload


def _inventory_record(chain) -> dict:
    return {
        "chain": chain.to_dict(),
        "reliable": chain.reliable,
        "issues": list(chain.issues),
    }


def _read_eq_from_chain(client, chain) -> dict | None:
    if not chain.reliable:
        return None
    matches = find_plugins(chain, plugin_id=contract.CHANNEL_EQ_PLUGIN_ID)
    if not matches:
        return None
    if len(matches) > 1:
        return {"state": None,
                "error": "more than one Channel EQ on the target"}
    slot = matches[0]
    if not slot.has_ref:
        return {"state": None,
                "error": "Channel EQ has no plugin_insert_ref; state is unreadable",
                "insert": slot.insert}
    state = _read_eq_state(client, slot.plugin_insert_ref)
    return {"insert": slot.insert, "target_ref": slot.plugin_insert_ref,
            "state": state}


def _export_plan(client, project: str, export_root: str) -> dict:
    result = _call(client, contract.TOOL_PROJECT, "export_plan", {
        "projects": [project],
        "output_root": export_root,
        "artifact": "bounce",
        "collision_policy": "fail_if_exists",
    }, timeout=mcp_client.EXPORT_TIMEOUT)
    return _result_record(result)


def _write_probe(client, project: str, track_index: int, track_name: str,
                 result: dict) -> dict:
    selector = {"track": track_index}
    key = "track:%d" % track_index
    chain = fetch_chain(client, selector, key=key)
    result["inventory_before_write"] = chain.to_dict()
    if not chain.reliable:
        raise _Abort("the track inventory is not reliable: %s"
                     % (", ".join(chain.issues) or chain.state or "unavailable"))
    existing = find_plugins(chain, plugin_id=contract.CHANNEL_EQ_PLUGIN_ID)
    if existing:
        raise _Abort(
            "track %d already has a Channel EQ (insert %s); use a fresh empty "
            "scratch track so this probe never reuses an existing EQ"
            % (track_index, existing[0].insert))
    empty = first_empty_slot(chain)
    if empty is None:
        raise _Abort("track %d has no verified-empty insert slot" % track_index)
    if not os.path.isabs(project):
        raise _Abort("--project must be an absolute .logicx path", 2)

    insert_params = {
        "insert": empty.insert,
        "plugin": "Channel EQ",
        "track": track_index,
        "expected_name": track_name,
        "mode": contract.MODE,
        "project_expected_path": project,
    }
    inserted = _call(client, contract.TOOL_PLUGINS, "insert_verified",
                     insert_params, timeout=mcp_client.WRITE_TIMEOUT)
    result["insert"] = _result_record(inserted)
    if not inserted.state_a:
        raise _Abort("insert_verified did not reach State A; no parameter "
                     "was written and no retry is attempted")
    result["project_mutated"] = True  # no rollback exists
    result["safe_to_retry"] = False

    refreshed = fetch_chain(client, selector, key=key)
    result["inventory_after_insert"] = refreshed.to_dict()
    slot, issue = verify_insert(refreshed, empty.insert,
                                contract.CHANNEL_EQ_PLUGIN_ID)
    if issue is not None:
        raise _Abort("the inserted Channel EQ could not be verified: %s" % issue)
    if not slot.has_ref:
        raise _Abort("the inserted Channel EQ has no plugin_insert_ref; "
                     "verified parameter writes require one")
    ref = slot.plugin_insert_ref
    result["target_ref"] = ref

    before = _read_eq_state(client, ref)
    band = _state_band(before, PEAK_BAND)
    if band is None:
        raise _Abort("%s is not reported by the Channel EQ" % PEAK_BAND)
    enabled = band.get("enabled") or {}
    if enabled.get("read_status") != "read" or \
            enabled.get("observed_raw") is not True:
        raise _Abort("%s is not verifiably enabled; no write was attempted"
                     % PEAK_BAND)

    writes = []
    for parameter, value, unit in (
            ("Frequency", TEST_FREQ_HZ, "Hz"),
            ("Gain", TEST_GAIN_DB, "dB"),
            ("Q", TEST_Q, "Q")):
        params = {
            "band": PEAK_BAND,
            "insert": slot.insert,
            "target_ref": ref,
            "parameter": parameter,
            "value": value,
            "unit": unit,
            "mode": contract.MODE,
            "project_expected_path": project,
        }
        written = _call(client, contract.TOOL_PLUGINS, "set_eq_band_verified",
                        params, timeout=mcp_client.WRITE_TIMEOUT)
        writes.append({"parameter": parameter, "value": value,
                       "result": _result_record(written)})
        if not written.state_a:
            result["writes"] = writes
            raise _Abort("set_eq_band_verified(%s) did not reach State A; "
                         "stopping, with no retry" % parameter)
    result["writes"] = writes

    after = _read_eq_state(client, ref)
    result["readback"] = after
    gain = _state_band(after, PEAK_BAND) or {}
    observed = gain.get("gain") or {}
    value = observed.get("observed_raw")
    if observed.get("read_status") != "read" or not isinstance(value, (int, float)):
        raise _Abort("%s gain could not be read back" % PEAK_BAND)
    if abs(float(value) - TEST_GAIN_DB) > 0.1:
        raise _Abort("readback %.2f dB != requested %.2f dB"
                     % (float(value), TEST_GAIN_DB))
    result["readback_ok"] = True
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="adapter.live_check",
        description="Manual live probe. Requires Logic open; writes only with "
                    "--write --confirm and only to a track.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--project", required=True,
                        help="absolute path to a DISPOSABLE scratch .logicx")
    parser.add_argument("--track", default="",
                        help="key=index:Name, e.g. vocal=3:Lead Vocal")
    parser.add_argument("--master", default="Stereo Out",
                        help="output strip name (read-only; never written)")
    parser.add_argument("--export-root", default=None,
                        help="absolute path for the read-only export_plan "
                             "(default: cwd)")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)

    if args.write and not args.confirm:
        print(json.dumps({"error": "--write requires --confirm (scratch project "
                                   "only; no rollback)"}))
        return 2
    if args.confirm and not args.write:
        print(json.dumps({"error": "--confirm has no effect without --write"}))
        return 2
    if not os.path.isabs(args.project or ""):
        print(json.dumps({"error": "--project must be an absolute path"}))
        return 2
    if args.write:
        print(json.dumps({"warning": SCRATCH_WARNING}))

    try:
        tracks = _parse_targets(args.track)
        master = _parse_master(args.master)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    if args.write and len(tracks) != 1:
        print(json.dumps({"error": "--write requires exactly one --track; the "
                                   "master strip is never a write target"}))
        return 2

    export_root = args.export_root or os.getcwd()
    if not os.path.isabs(export_root):
        print(json.dumps({"error": "--export-root must be an absolute path"}))
        return 2

    report = {
        "tool": "adapter.live_check",
        "read_only": not args.write,
        "project": args.project,
        "export_root": export_root,
        "readback_ok": None,
    }
    try:
        client = mcp_client.connect(args.endpoint)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": "cannot reach the bridge: %s" % exc}))
        return 1

    try:
        try:
            report["health"] = client.health_check()
        except Exception as exc:  # noqa: BLE001
            raise _Abort("health check failed: %s" % exc)

        report["inventories"] = {}
        chains = {}
        for key, target in tracks.items():
            chain = fetch_chain(client, {"track": target["index"]}, key=key)
            chains[key] = chain
            report["inventories"][key] = _inventory_record(chain)
        master_chain = fetch_chain(client, contract.MASTER_SELECTOR, key="master")
        report["inventories"]["master"] = _inventory_record(master_chain)

        report["channel_eq"] = {}
        for key, chain in chains.items():
            try:
                eq = _read_eq_from_chain(client, chain)
            except _Abort as exc:
                eq = {"error": exc.message}
            if eq is not None:
                report["channel_eq"][key] = eq
        try:
            master_eq = _read_eq_from_chain(client, master_chain)
        except _Abort as exc:
            master_eq = {"error": exc.message}
        if master_eq is not None:
            report["channel_eq"]["master"] = master_eq

        report["export_plan"] = _export_plan(client, args.project, export_root)

        if args.write:
            key, target = next(iter(tracks.items()))
            report["write"] = {"track_key": key,
                               "track": target["index"],
                               "name": target["name"]}
            try:
                _write_probe(client, args.project, target["index"],
                             target["name"], report["write"])
                report["readback_ok"] = True
            except _Abort as exc:
                report["write"]["error"] = exc.message
                report["exit"] = exc.exit_code
                print(json.dumps(report, indent=1, ensure_ascii=False,
                                 default=str))
                return exc.exit_code
        report["exit"] = 0
        print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
        return 0
    except _Abort as exc:
        report["error"] = exc.message
        report["exit"] = exc.exit_code
        print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
        return exc.exit_code
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
