"""Measure dB <-> mixer contract on the live session, then write a calibration file.

For each contract value it calls logic_mixer.set_volume and reads the level back from the
`logic://mixer` resource, so the resulting table is measured, not assumed. It MOVES FADERS:
run it on a scratch track, never on a mix you care about.

    python -m adapter.calibrate_fader --track 7 --out fader.json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

CONTRACTS = [0.2, 0.4, 0.5, 0.6, 0.8]


def _rpc(endpoint: str, method: str, params: dict, timeout: int = 30) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--track", type=int, required=True, help="scratch track index")
    parser.add_argument("--out", default="fader-calibration.json")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()

    points = []
    for contract in CONTRACTS:
        _rpc(args.endpoint, "tools/call",
             {"name": "logic_mixer",
              "arguments": {"command": "set_volume",
                            "params": {"track": args.track, "value": contract}}})
        mixer = _rpc(args.endpoint, "resources/read", {"uri": "logic://mixer"})
        text = mixer.get("result", {}).get("contents", [{}])[0].get("text", "{}")
        payload = json.loads(text)
        tracks = payload.get("tracks") or payload.get("strips") or []
        level = next((t.get("volume") for t in tracks if t.get("index") == args.track), None)
        if level is None:
            print(f"contract {contract}: could not read the level back", file=sys.stderr)
            continue
        points.append([float(level), contract])
        print(f"contract {contract} -> {level} dB")
    if len(points) < 3:
        print("not enough points; nothing written", file=sys.stderr)
        return 1
    json.dump({"points": points, "source": "adapter.calibrate_fader"}, open(args.out, "w"), indent=1)
    print(f"wrote {args.out} with {len(points)} points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
