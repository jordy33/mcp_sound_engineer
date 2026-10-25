"""CLI: plan (and optionally apply) a recipe against the running mcp_logic bridge."""
from __future__ import annotations

import argparse
import json
import sys

from .logic_adapter import apply, plan, summary


def _parse_targets(text: str) -> dict[str, dict]:
    """`role=index:Track Name` pairs -> {role: {index, name}}."""
    out: dict[str, dict] = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        role, _, rest = pair.partition("=")
        index, _, name = rest.partition(":")
        out[role.strip()] = {"index": int(index), "name": name.strip()}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(prog="adapter")
    parser.add_argument("--recipe", required=True, help="recipe JSON file")
    parser.add_argument("--tracks", default="", help="role=index:Name, e.g. vocal=3:Lead Vocal")
    parser.add_argument("--master", default="", help="index:Name of the master strip")
    parser.add_argument("--project", required=True, help="absolute path to the .logicx package")
    parser.add_argument("--apply", action="store_true", help="execute the plan (default: dry run)")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()

    recipe = json.load(open(args.recipe))
    tracks = _parse_targets(args.tracks)
    master = _parse_targets(args.master).get("master") if args.master else None

    steps = plan(recipe, tracks, master, args.project)
    print(json.dumps({"dry_run": not args.apply, "summary": summary(steps), "steps": steps},
                     indent=1, ensure_ascii=False))
    if not args.apply:
        return 0
    results = apply(steps, args.endpoint)
    print(json.dumps({"applied": results}, indent=1, ensure_ascii=False))
    return 0 if all(r.get("status") != "error" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
