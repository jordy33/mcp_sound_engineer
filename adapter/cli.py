"""CLI: plan (and optionally apply) a recipe against the running mcp_logic bridge.

Dry runs are the default.  ``--apply`` is required for any write.  Inventory is
read from ``--inventory FILE`` for a dry run or live with ``--fetch-inventory``;
an apply always reads the live inventory for its preflight and re-reads it after
an insert, never trusting the file.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import mcp_client
from . import mcp_logic_contract as contract
from .fader_calibration import FaderCalibration
from .inventory import InventoryFile, fetch_chain
from .logic_adapter import (
    RecipeError,
    apply,
    build_plan,
    inventory_targets,
    summary,
)

BLOCKING_STATUSES = frozenset({"unresolved", "needs_inventory", "needs_calibration"})


def _parse_targets(text: str) -> dict[str, dict]:
    """`key=index:Track Name` pairs -> {key: {index, name}}.

    The key may be a role (`vocal=3:Lead Vocal`) or the exported basename
    (`01_bass.wav=2:Bass`), which is what a recipe's `stem` field carries.
    """
    out: dict[str, dict] = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        role, sep, rest = pair.partition("=")
        if not sep or not role.strip():
            raise ValueError("track mapping %r needs key=index:Name" % pair)
        index, colon, name = rest.partition(":")
        if not colon:
            raise ValueError("track mapping %r needs key=index:Name" % pair)
        try:
            number = int(index)
        except ValueError:
            raise ValueError("track index %r is not an integer" % index)
        out[role.strip()] = {"index": number, "name": name.strip()}
    return out


def _parse_master(text: str) -> dict | None:
    """Bare name or `index:Name` (optionally `master=...`) -> {index, name}.

    The Stereo Output strip has no Arrange index; an index here is accepted for
    backward compatibility and only recorded, never emitted.
    """
    if not text:
        return None
    _, sep, rest = text.rpartition("=")
    value = rest if sep else text
    index_text, colon, name = value.partition(":")
    if colon and index_text.strip().isdigit():
        return {"index": int(index_text.strip()), "name": name.strip()}
    return {"index": None, "name": value.strip()}


def _load_inventory(args, recipe, tracks, master):
    """Build the dry-run inventory mapping (never used by an apply)."""
    targets = inventory_targets(recipe, tracks, master)
    if args.inventory:
        try:
            source = InventoryFile.load(args.inventory)
        except (OSError, ValueError) as exc:
            raise ValueError("cannot read --inventory %s: %s"
                             % (args.inventory, exc))
        single_target = len(targets) == 1
        chains = {}
        for entry in targets:
            chain = source.chain_for(
                entry["keys"], single_target=single_target,
                selector=_selector(entry["target"]))
            if chain is not None:
                chains[entry["target"]["key"]] = chain
        return chains

    if not (args.fetch_inventory or args.apply):
        return None
    if not targets:
        return None
    try:
        client = mcp_client.connect(args.endpoint)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("cannot read the live inventory: %s" % exc)
    chains = {}
    try:
        for entry in targets:
            selector = _selector(entry["target"])
            chain = fetch_chain(client, selector,
                                key=entry["target"]["key"])
            chains[entry["target"]["key"]] = chain
    finally:
        client.close()
    return chains


def _selector(target: dict) -> dict:
    return (dict(contract.MASTER_SELECTOR) if target.get("is_master")
            else {"track": target["track"]})


def _print(payload) -> None:
    print(json.dumps(payload, indent=1, ensure_ascii=False, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(prog="adapter")
    parser.add_argument("--recipe", required=True, help="recipe JSON file")
    parser.add_argument("--tracks", default="",
                        help="role-or-basename=index:Name, e.g. vocal=3:Lead Vocal "
                             "or 01_bass.wav=2:Bass")
    parser.add_argument("--master", default="",
                        help="'Stereo Out' or 'index:Name' (the index is informational)")
    parser.add_argument("--project", required=True,
                        help="absolute path to the .logicx package")
    parser.add_argument("--inventory", default=None,
                        help="dry-run inventory JSON (never used by --apply)")
    parser.add_argument("--fetch-inventory", action="store_true",
                        help="read the live inventory (read-only) before planning")
    parser.add_argument("--overwrite-eq", action="store_true",
                        help="reuse an existing Channel EQ on a track (never on master)")
    parser.add_argument("--apply", action="store_true",
                        help="execute the plan (default: dry run)")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="continue independent targets after a non-State-A result")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--fader-calibration", default=None,
                        help="dB->contract points measured with adapter.calibrate_fader")
    args = parser.parse_args()

    if args.inventory and args.apply:
        _print({"error": "--inventory is dry-run only; an apply re-reads the "
                         "live inventory (use --fetch-inventory to inspect it)"})
        return 1

    try:
        with open(args.recipe, encoding="utf-8") as handle:
            recipe = json.load(handle)
    except (OSError, ValueError) as exc:
        _print({"error": "cannot read recipe %s: %s" % (args.recipe, exc)})
        return 2
    try:
        tracks = _parse_targets(args.tracks)
        master = _parse_master(args.master)
    except ValueError as exc:
        _print({"error": str(exc)})
        return 2

    try:
        inventory = _load_inventory(args, recipe, tracks, master)
    except ValueError as exc:
        _print({"error": str(exc)})
        return 2

    try:
        steps = build_plan(recipe, tracks, master, args.project,
                           FaderCalibration.load(args.fader_calibration),
                           inventory=inventory,
                           overwrite_eq=args.overwrite_eq)
    except RecipeError as exc:
        _print({"error": str(exc), "violations": exc.violations})
        return 2

    report = {
        "dry_run": not args.apply,
        "adapter_contract_version": contract.ADAPTER_CONTRACT_VERSION,
        "summary": summary(steps),
        "steps": steps,
    }
    _print(report)
    if not args.apply:
        return 0

    blocked = [{"index": index, "action": step.get("action"),
                "status": step.get("status"), "why": step.get("why")}
               for index, step in enumerate(steps)
               if step.get("status") in BLOCKING_STATUSES]
    if blocked:
        _print({"error": "plan_blocked: resolve these steps before applying",
                "blocked": blocked})
        return 1

    results = apply(steps, args.endpoint,
                    continue_on_error=args.continue_on_error)
    _print({"applied": results})
    failed = [result for result in results
              if result.get("status") in ("error", "partial", "skipped_dependency")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
