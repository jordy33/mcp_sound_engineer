"""CLI: plan (and optionally apply) a recipe against the running mcp_logic bridge."""
from __future__ import annotations

import argparse
import json
import sys

from .logic_adapter import apply, plan, summary


def main() -> int:
    parser = argparse.ArgumentParser(prog="adapter")
    parser.add_argument("--recipe", required=True, help="recipe JSON file")
    parser.add_argument("--tracks", default="", help="role=index pairs, e.g. vocal=3,drums=1")
    parser.add_argument("--master", type=int, default=None, help="track index used as the master")
    parser.add_argument("--apply", action="store_true", help="execute the plan (default: dry run)")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()

    recipe = json.load(open(args.recipe))
    tracks = {}
    for pair in filter(None, args.tracks.split(",")):
        role, _, index = pair.partition("=")
        tracks[role.strip()] = int(index)

    steps = plan(recipe, tracks, args.master)
    print(json.dumps({"dry_run": not args.apply, "summary": summary(steps), "steps": steps},
                     indent=1, ensure_ascii=False))
    if not args.apply:
        return 0
    results = apply(steps, args.endpoint)
    print(json.dumps({"applied": results}, indent=1, ensure_ascii=False))
    return 0 if all(r.get("status") != "error" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
