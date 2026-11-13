"""Measure dB <-> mixer contract on the live session, then write a calibration file.

For each contract value it calls logic_mixer.set_volume and reads the level back from the
`logic://mixer` resource, so the resulting table is measured, not assumed. It MOVES FADERS:
run it on a scratch track, never on a mix you care about.

    python -m adapter.calibrate_fader --track 7 --out fader.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys

try:
    from . import mcp_client, mcp_logic_contract as contract
except ImportError:
    import mcp_client
    import mcp_logic_contract as contract

CONTRACTS = [0.2, 0.4, 0.5, 0.6, 0.8]


def _is_finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def calibrate(track: int, out: str = "fader-calibration.json",
              endpoint: str = "http://127.0.0.1:8765/mcp",
              contracts: list[float] | None = None,
              client=None) -> int:
    contracts = contracts if contracts is not None else CONTRACTS
    owning = client is None
    if owning:
        client = mcp_client.connect(endpoint)

    points: list[list[float]] = []
    try:
        for contract_val in contracts:
            res = client.call_tool(
                contract.TOOL_MIXER,
                {"command": "set_volume",
                 "params": {"track": track, "value": contract_val}})
            if not getattr(res, "applied", False):
                reason = (getattr(res, "reason", None) or getattr(res, "error", None)
                          or getattr(res, "state", "not State A"))
                print(f"contract {contract_val}: write did not reach State A ({reason}); skipping",
                      file=sys.stderr)
                continue

            mixer_res = client.read_resource("logic://mixer")
            if not getattr(mixer_res, "ok", False):
                print(f"contract {contract_val}: could not read logic://mixer ({getattr(mixer_res, 'error', 'unknown error')})",
                      file=sys.stderr)
                continue

            payload = getattr(mixer_res, "payload", {}) or {}
            tracks = payload.get("tracks") or payload.get("strips") or []
            entry = next((t for t in tracks if isinstance(t, dict) and t.get("index") == track), None)
            if entry is None:
                print(f"contract {contract_val}: track {track} not found in logic://mixer",
                      file=sys.stderr)
                continue

            vol = entry.get("volume")
            if not _is_finite_number(vol):
                print(f"contract {contract_val}: non-numeric volume {vol!r} read back for track {track}",
                      file=sys.stderr)
                continue

            level_db = float(vol)
            points.append([level_db, contract_val])
            print(f"contract {contract_val} -> {level_db} dB")
    finally:
        if owning:
            client.close()

    if len(points) < 3:
        print("not enough points; nothing written", file=sys.stderr)
        return 1

    with open(out, "w") as f:
        json.dump({"points": points, "source": "adapter.calibrate_fader"}, f, indent=1)
    print(f"wrote {out} with {len(points)} points")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--track", type=int, required=True, help="scratch track index")
    parser.add_argument("--out", default="fader-calibration.json")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args(argv)
    return calibrate(args.track, args.out, args.endpoint)


if __name__ == "__main__":
    sys.exit(main())
