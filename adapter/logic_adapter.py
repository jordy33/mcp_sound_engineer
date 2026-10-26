"""Translate a DAW-neutral recipe into calls mcp_logic actually verifies.

The adapter is deliberately narrower than the recipe vocabulary:

* ``fader`` and ``pan`` use ``logic_mixer`` (track bound; the master strip is
  not addressable there).
* ``eq.bell`` and ``eq.shelf`` write the Channel EQ's verified 18 parameters
  (``Frequency``/``Gain``/``Q`` on Low Shelf, Peak 1-4, High Shelf).  Low Cut
  and High Cut are observed-only, so high-pass actions are manual.
* Every other action is manual: Limiter is not insertable at all, Compressor
  verifies only threshold/limiter_on/auto_release, and polarity/dither/de-esser/
  stereo width/dynamic EQ have no verified write surface.

Addressing comes from the inventory, never from a hard-coded slot:

* master: ``strip: "output"`` plus the published ``expected_name`` contract;
  never an Arrange index and never ``target_ref``.
* track: the first verified-empty slot for an insert, then the slot's
  ``plugin_insert_ref`` for parameter writes.

An existing master EQ is never reused: the output strip emits no ref, so its
band state cannot be read back.  An existing track EQ requires
``overwrite_eq=True`` and is rebound through its ref.

Planning is pure (no network).  ``apply_steps`` sends the calls and treats only
an explicit Honest Contract State A as applied; State B/C and transport errors
stop the chain.  No write is ever retried.
"""
from __future__ import annotations

import math
import os
import time

from server import rules

from . import mcp_client
from . import mcp_logic_contract as contract
from .inventory import (
    InventoryChain,
    InventoryFile,
    first_empty_slot,
    from_tool_result,
    find_plugins,
    parse_inventory,
    verify_insert,
)
from .mcp_logic_contract import CHANNEL_EQ_PLUGIN_ID

MODE = contract.MODE
EQ_PARAMETER = contract.EQ_PARAMETER
MANUAL_HINT = dict(contract.MANUAL_REASONS)

CAPABILITIES: dict[str, str] = {}
CAPABILITIES.update({name: "ready" for name in contract.READY_ACTIONS})
CAPABILITIES.update({name: "manual" for name in contract.MANUAL_ACTIONS})
CAPABILITIES.update({name: "terminal" for name in contract.TERMINAL_ACTIONS})
# Defensive: a recipe action added to rules.py before the contract is updated
# must never silently become automatable.
for _action_name in rules.ACTIONS:
    CAPABILITIES.setdefault(_action_name, "manual")

EQ_ACTIONS = frozenset({"eq.bell", "eq.shelf"})
PHASE_BY_ACTION = {
    "fader": "static",
    "pan": "static",
    "eq.shelf": "static",   # shelves are a broad static move
    "eq.bell": "bell",      # narrow moves are measured and corrected
}
_PHASE_ORDER = {"static": 0, "bell": 1}

MASTER_FADER_HINT = (
    "logic_mixer.set_volume is track-bound; there is no verified master fader "
    "(set_master_volume is MCU State B)")
MASTER_PAN_HINT = (
    "logic_mixer.set_pan is track-bound; there is no verified master pan")
MASTER_EQ_HINT = (
    "the Stereo Output strip emits no plugin_insert_ref, so mcp_logic cannot "
    "read back an existing master Channel EQ; it is never reused or overwritten")

REFRESH_ATTEMPTS = 3
REFRESH_DELAY = 0.5


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


class RecipeError(ValueError):
    """The recipe violates the server-side schema or gate rules."""

    def __init__(self, violations):
        self.violations = list(violations)
        detail = "; ".join(
            "%s%s" % (v.get("code"), " at action %s" % v["index"]
                      if "index" in v else "")
            for v in self.violations) or "invalid recipe"
        super().__init__("recipe rejected (schema %s): %s"
                         % (rules.RECIPE_SCHEMA_VERSION, detail))


# ---------------------------------------------------------------------------
# public planning API
# ---------------------------------------------------------------------------
def db_to_fader(db: float, calibration=None) -> float | None:
    """dB -> mixer contract, via a measured calibration.

    The mixer takes a contract value with a measured taper, not linear
    amplitude: 10**(dB/20) is wrong, and 1.0 is not necessarily 0 dB.  Without
    at least three measured points the adapter refuses to guess (returns None).
    """
    if calibration is None or not getattr(calibration, "complete", False):
        return None
    return calibration.to_contract(db)


def build_plan(recipe, tracks=None, master=None, project_path=None,
               fader_calibration=None, *, inventory=None,
               overwrite_eq=False) -> list[dict]:
    """Validate a recipe, then plan it.  The only entry point used by the CLI/loop."""
    violations = rules.validate_recipe(recipe)
    if isinstance(recipe, list):
        for index, action in enumerate(recipe):
            if isinstance(action, dict) and \
                    action.get("schema") != rules.RECIPE_SCHEMA_VERSION:
                violation = {"code": "schema_mismatch", "index": index,
                             "schema": action.get("schema")}
                if violation not in violations:
                    violations.append(violation)
    if violations:
        raise RecipeError(violations)
    return plan(recipe, tracks, master, project_path, fader_calibration,
                inventory=inventory, overwrite_eq=overwrite_eq)


def plan(recipe: list[dict], tracks: dict[str, dict] | None = None,
         master: dict | None = None, project_path: str | None = None,
         fader_calibration=None, *, inventory=None,
         overwrite_eq: bool = False) -> list[dict]:
    """Recipe -> ordered steps with explicit, verified-surface calls.

    Pure: no network, no session changes.  ``inventory`` is a mapping of target
    aliases to :class:`InventoryChain` objects or raw ``get_inventory`` payloads,
    or an :class:`InventoryFile` for a dry run.  ``apply_steps`` always re-reads
    the live inventory instead.
    """
    tracks = tracks or {}
    actions = recipe if isinstance(recipe, list) else []
    resolved = [_resolve_target(action, tracks, master)
                if isinstance(action, dict) else
                (None, "recipe entry is not an object")
                for action in actions]

    unique_targets = {_group_key(target) for target, _ in resolved if target}
    single_target = len(unique_targets) == 1

    groups: dict[str, list[int]] = {}
    for index, (action, (target, _why)) in enumerate(zip(actions, resolved)):
        if target is not None and action.get("action") in EQ_ACTIONS:
            groups.setdefault(_group_key(target), []).append(index)

    prepared = {
        key: _prepare_eq_group(actions, resolved, indices, inventory,
                               project_path, overwrite_eq, single_target)
        for key, indices in groups.items()
    }

    steps: list[dict] = []
    for index, action in enumerate(actions):
        steps.append(_build_step(index, action, resolved[index], prepared,
                                 project_path, fader_calibration))
    return steps


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------
def _stem_mapping(action: dict, tracks: dict) -> tuple:
    stem = action.get("stem")
    if not isinstance(stem, str) or not stem:
        return None, None
    base = os.path.basename(stem)
    if not isinstance(tracks, dict):
        return None, base
    return (tracks.get(base) or tracks.get(os.path.splitext(base)[0])), base


def _track_target(mapping, *, key, role, target_text, label) -> tuple:
    if not isinstance(mapping, dict):
        return None, "track mapping for %s must be an object" % label
    index = mapping.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        return None, "track mapping for %s needs an Int index >= 0" % label
    name = mapping.get("name")
    name = name.strip() if isinstance(name, str) else ""
    return ({
        "key": key or ("track:%d" % index),
        "target_text": target_text,
        "is_master": False,
        "track": index,
        "name": name,
        "role": role,
    }, None)


def _resolve_target(action: dict, tracks: dict, master: dict | None) -> tuple:
    """Return ``(target, why_not)``.  Only ``master`` maps to the output strip."""
    text = (action.get("target") or "master").lower()
    if text == "master":
        name = ""
        if isinstance(master, dict) and isinstance(master.get("name"), str):
            name = master["name"].strip()
        return ({
            "key": "master",
            "target_text": "master",
            "is_master": True,
            "strip": contract.MASTER_STRIP,
            "track": None,
            "name": name or contract.MASTER_EXPECTED_NAME,
            "role": "master",
        }, None)

    if text in ("mix_bus", "bus"):
        mapping = None
        if isinstance(tracks, dict):
            if "mix_bus" in tracks:
                mapping = tracks["mix_bus"]
            elif "bus" in tracks:
                mapping = tracks["bus"]
        if mapping is None:
            return None, (
                "no track for target %r; mix_bus/bus are never mapped to the "
                "master strip; pass --tracks mix_bus=<index>:<Track Name>" % text)
        return _track_target(mapping, key=text, role=text, target_text=text,
                             label=text)

    if text == "stem":
        mapping, base = _stem_mapping(action, tracks)
        if mapping is None:
            return None, (
                "no track for stem %r; pass --tracks %s=<index>:<Track Name>"
                % (action.get("stem"), base or "<basename>"))
        return _track_target(mapping, key=base, role=None, target_text="stem",
                             label="stem %r" % base)

    mapping, base = _stem_mapping(action, tracks)
    if mapping is None and isinstance(tracks, dict):
        mapping = tracks.get(text)
    if mapping is None:
        return None, (
            "no track for target %r; pass --tracks "
            "<role-or-basename>=<index>:<Track Name>" % text)
    return _track_target(mapping, key=base or text, role=text,
                         target_text=text, label=text)


def _group_key(target: dict) -> str:
    return "master" if target["is_master"] else "track:%d" % target["track"]


def _selector(target: dict) -> dict:
    return (dict(contract.MASTER_SELECTOR) if target["is_master"]
            else {"track": target["track"]})


def _label(target: dict) -> str:
    if target["is_master"]:
        return "the master Stereo Output"
    return "track %d (%r)" % (target["track"], target["name"] or "unnamed")


# ---------------------------------------------------------------------------
# inventory lookup (dry run / tests)
# ---------------------------------------------------------------------------
def _inventory_keys(target: dict, actions: list[dict]) -> list[str]:
    if target["is_master"]:
        return ["master"]
    keys: list[str] = []
    for action in actions:
        stem = action.get("stem")
        if isinstance(stem, str) and stem:
            base = os.path.basename(stem)
            keys.extend([base, os.path.splitext(base)[0]])
        text = (action.get("target") or "").lower()
        if text:
            keys.append(text)
        if text in ("mix_bus", "bus"):
            keys.extend(["mix_bus", "bus"])
    role = target.get("role")
    if role:
        keys.append(role)
    keys.extend([target["key"], "track:%d" % target["track"]])
    seen = set()
    ordered = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _chain_for(inventory, keys, target, single_target):
    """Resolve one inventory chain.  Returns ``(chain_or_None, note)``."""
    if inventory is None:
        return None, "no inventory supplied"
    selector = _selector(target)
    if isinstance(inventory, InventoryFile):
        return inventory.chain_for(keys, single_target=single_target,
                                   selector=selector), "inventory file"
    if isinstance(inventory, dict):
        for key in [_group_key(target)] + list(keys):
            if key not in inventory:
                continue
            value = inventory[key]
            if isinstance(value, InventoryChain):
                return value, key
            if isinstance(value, dict):
                return parse_inventory(value, key=key, selector=selector), key
        return None, "no inventory key matched %s" % ", ".join(keys)
    raise TypeError("inventory must be a mapping, an InventoryFile or None")


def inventory_targets(recipe, tracks: dict[str, dict] | None = None,
                      master: dict | None = None) -> list[dict]:
    """Targets that need an inventory read, resolved exactly like ``plan``.

    The CLI uses this to build ``--fetch-inventory``/``--inventory`` chains
    without duplicating target resolution.  Manual actions are excluded: they
    never emit a plugin call.
    """
    tracks = tracks or {}
    actions = recipe if isinstance(recipe, list) else []
    groups: dict[str, dict] = {}
    for action in actions:
        if not isinstance(action, dict) or action.get("action") not in EQ_ACTIONS:
            continue
        target, why = _resolve_target(action, tracks, master)
        if target is None:
            continue
        key = _group_key(target)
        group = groups.setdefault(key, {"target": target, "actions": [],
                                        "keys": [], "why": []})
        group["actions"].append(action)
        if why:
            group["why"].append(why)
    for group in groups.values():
        group["keys"] = _inventory_keys(group["target"], group["actions"])
    return list(groups.values())


# ---------------------------------------------------------------------------
# Channel EQ preparation: bands, slot, binding
# ---------------------------------------------------------------------------
def _assign_bands(actions: list[dict]) -> dict[int, dict]:
    """Assign a unique verified band to each EQ step, or explain why not."""
    result = {index: {"band": None, "reason": None, "why": None}
              for index in range(len(actions))}
    explicit_count: dict[str, int] = {}
    for action in actions:
        band = action.get("band_name")
        if isinstance(band, str) and band.strip():
            explicit_count[band] = explicit_count.get(band, 0) + 1

    reserved: set[str] = set()
    for index, action in enumerate(actions):
        name = action.get("action")
        band = action.get("band_name")
        item = result[index]
        if band is not None:
            allowed = contract.BELL_BANDS if name == "eq.bell" else contract.SHELF_BANDS
            if not isinstance(band, str) or not band.strip():
                item.update(reason="band_name_invalid",
                            why="band_name must be a non-empty string")
                continue
            if band not in allowed:
                item.update(
                    reason="band_not_verified",
                    why=("%r is not a verified %s band; use one of %s"
                         % (band, "bell" if name == "eq.bell" else "shelf",
                            ", ".join(allowed))))
                continue
            if explicit_count.get(band, 0) > 1:
                item.update(
                    reason="band_collision",
                    why="band %r is requested by more than one step on this target"
                        % band)
                continue
            reserved.add(band)
            item["band"] = band

    for index, action in enumerate(actions):
        item = result[index]
        if item["reason"] is not None:
            continue
        name = action.get("action")
        problem = _eq_parameter_problem(action)
        if problem is not None:
            item.update(reason=problem[0], why=problem[1])
            continue
        if name == "eq.shelf" and item["band"] is None:
            item.update(
                reason="shelf_band_required",
                why=("an eq.shelf step must name 'Low Shelf' or 'High Shelf' in "
                     "band_name; the adapter never guesses the shelf side"))
            continue
        if name == "eq.bell" and item["band"] is None:
            free = next((band for band in contract.BELL_BANDS
                         if band not in reserved), None)
            if free is None:
                item.update(
                    reason="no_free_band",
                    why=("all four Peak bands are already assigned on this "
                         "target; name the bands explicitly and split the moves"))
                continue
            reserved.add(free)
            item["band"] = free
    return result


def _eq_parameter_problem(action: dict):
    if not _is_number(action.get("freq_hz")) or float(action["freq_hz"]) <= 0:
        return ("eq_frequency_invalid",
                "eq.bell/eq.shelf need a positive numeric freq_hz")
    if not _is_number(action.get("gain_db")):
        return ("eq_gain_invalid", "eq.bell/eq.shelf need a numeric gain_db")
    if "q" in action and (not _is_number(action.get("q"))
                          or float(action["q"]) <= 0):
        return ("eq_q_invalid", "q must be a positive number when supplied")
    return None


def _bind_eq(chain, target: dict, overwrite_eq: bool) -> dict:
    """Decide whether this target can take the planned EQ writes."""
    if chain is None:
        return {
            "status": "needs_inventory",
            "reason": "inventory_required",
            "why": ("no inventory for %s; pass --inventory <file> for a dry run "
                    "or --fetch-inventory to read it live" % _label(target)),
        }
    if not chain.reliable:
        detail = ", ".join(chain.issues) or chain.state or "unreadable"
        return {
            "status": "needs_inventory",
            "reason": "inventory_not_reliable",
            "why": ("inventory for %s is not usable (%s); re-read it before "
                    "writing" % (_label(target), detail)),
        }

    existing = find_plugins(chain, plugin_id=CHANNEL_EQ_PLUGIN_ID)
    if len(existing) > 1:
        return {
            "status": "unresolved",
            "reason": "duplicate_channel_eq",
            "why": ("%s already has more than one Channel EQ; this adapter never "
                    "guesses which one to write" % _label(target)),
        }

    if existing:
        slot = existing[0]
        if slot.bypassed is not False:
            return {
                "status": "unresolved",
                "reason": "plugin_bypassed_or_unknown",
                "why": "the existing Channel EQ is bypassed or its bypass state is unknown",
            }
        if target["is_master"]:
            return {
                "status": "manual",
                "reason": "master_eq_state_unreadable",
                "hint": MASTER_EQ_HINT,
            }
        if not overwrite_eq:
            return {
                "status": "unresolved",
                "reason": "eq_overwrite_required",
                "why": ("%s already has a Channel EQ; pass --overwrite-eq to "
                        "reuse it" % _label(target)),
            }
        if not slot.has_ref:
            return {
                "status": "needs_inventory",
                "reason": "missing_plugin_insert_ref",
                "why": ("the existing Channel EQ on %s has no plugin_insert_ref; "
                        "apply must not write through a bare index"
                        % _label(target)),
            }
        return {
            "status": "ready", "inserted": False, "insert": slot.insert,
            "ref": slot.plugin_insert_ref, "pre_existing": True,
            "warnings": ["band_enablement_unverified"],
        }

    empty = first_empty_slot(chain)
    if empty is None:
        return {
            "status": "unresolved",
            "reason": "no_free_insert_slot",
            "why": "no verified-empty insert slot remains on %s" % _label(target),
        }
    warnings = ["band_enablement_unverified"] if target["is_master"] else []
    return {
        "status": "ready", "inserted": True, "insert": empty.insert,
        "ref": None, "pre_existing": False, "warnings": warnings,
    }


def _prepare_eq_group(actions, resolved, indices, inventory, project_path,
                      overwrite_eq, single_target) -> dict:
    target = dict(resolved[indices[0]][0])
    target["project_path"] = project_path
    group_actions = [actions[index] for index in indices]
    assignments = _assign_bands(group_actions)
    by_index = {index: assignments[position]
                for position, index in enumerate(indices)}
    valid = {index: item for index, item in by_index.items()
             if item["reason"] is None}
    prepared = {
        "target": target,
        "by_index": by_index,
        "calls": {},
        "warnings": {},
        "override": {},
        "binding": None,
    }
    if not valid:
        return prepared

    track_names = {resolved[index][0].get("name", "") for index in indices}
    if not target["is_master"] and len(track_names) > 1:
        _set_override(prepared, valid, "unresolved", "target_name_conflict",
                      "the same track index was mapped from conflicting names: %s"
                      % ", ".join(sorted(repr(name) for name in track_names)))
        return prepared

    if not project_path:
        _set_override(
            prepared, valid, "unresolved", "project_required",
            "verified plugin writes need --project <path.logicx>")
        return prepared

    if not target["is_master"] and not target["name"]:
        _set_override(
            prepared, valid, "unresolved", "track_name_required",
            "Channel EQ writes on a track need --tracks <key>=<index>:<Track Name>; "
            "insert_verified corroborates the name")
        return prepared

    chain, _note = _chain_for(inventory,
                              _inventory_keys(target, group_actions),
                              target, single_target)
    binding = _bind_eq(chain, target, overwrite_eq)
    prepared["binding"] = binding
    if binding["status"] != "ready":
        _set_override(prepared, valid, binding["status"], binding["reason"],
                      binding.get("why") or binding.get("hint"))
        return prepared

    for index in valid:
        calls = _eq_write_calls(
            target, actions[index], by_index[index]["band"],
            binding["insert"], binding["ref"])
        for call in calls:
            call["requires_insert"] = bool(binding["inserted"])
        prepared["calls"][index] = calls
        prepared["warnings"][index] = list(binding["warnings"])

    host = min(valid, key=lambda index: (
        0 if PHASE_BY_ACTION.get(actions[index].get("action")) == "static" else 1,
        index))
    if binding["inserted"]:
        prepared["calls"][host].insert(
            0, _insert_eq_call(target, binding["insert"]))
    return prepared


def _set_override(prepared: dict, indices, status: str, reason: str, why: str):
    for index in indices:
        prepared["override"][index] = {
            "status": status, "reason": reason, "why": why,
            "hint": why if status == "manual" else None,
        }


# ---------------------------------------------------------------------------
# call builders
# ---------------------------------------------------------------------------
def _base_write_params(project_path: str) -> dict:
    return {"mode": MODE, "project_expected_path": project_path}


def _insert_eq_call(target: dict, insert: int) -> dict:
    params = {"insert": insert, "plugin": "Channel EQ"}
    if target["is_master"]:
        params["strip"] = contract.MASTER_STRIP
        params["expected_name"] = target["name"] or contract.MASTER_EXPECTED_NAME
    else:
        params["track"] = target["track"]
        params["expected_name"] = target["name"]
    params.update(_base_write_params(target["project_path"]))
    return _call(contract.TOOL_PLUGINS, "insert_verified", params,
                 kind="insert", target=target, insert=insert,
                 requires_insert=False)


def _eq_write_calls(target: dict, action: dict, band: str, insert: int,
                    ref: str | None) -> list[dict]:
    params = {"band": band, "insert": insert}
    if target["is_master"]:
        params["strip"] = contract.MASTER_STRIP
    elif ref:
        # The reference is the authority; insert corroborates the slot.
        params["target_ref"] = ref
    else:
        params["track"] = target["track"]
    params.update(_base_write_params(target["project_path"]))

    wanted = [("freq", float(action["freq_hz"])),
              ("gain", float(action["gain_db"]))]
    if "q" in action:
        wanted.append(("q", float(action["q"])))

    calls = []
    for key, value in wanted:
        parameter, unit = EQ_PARAMETER[key]
        write = dict(params)
        write.update({"parameter": parameter, "value": value, "unit": unit})
        calls.append(_call(
            contract.TOOL_PLUGINS, "set_eq_band_verified", write,
            kind="write", target=target, insert=insert, band=band,
            requires_insert=False))
    return calls


def _call(tool: str, command: str, params: dict, *, kind: str, target: dict,
          insert: int | None = None, band: str | None = None,
          requires_insert: bool = False) -> dict:
    call = {
        "tool": tool,
        "arguments": {"command": command, "params": params},
        "kind": kind,
        "requires_insert": bool(requires_insert),
        "target_key": _group_key(target),
        "insert": insert,
    }
    if band is not None:
        call["band"] = band
    return call


def _mixer_call(command: str, target: dict, value: float) -> dict:
    params = {"track": target["track"], "value": value}
    return _call(contract.TOOL_MIXER, command, params, kind="write",
                 target=target, insert=None)


# ---------------------------------------------------------------------------
# step assembly
# ---------------------------------------------------------------------------
def _build_step(index, action, resolution, prepared, project_path,
                fader_calibration) -> dict:
    if not isinstance(action, dict):
        return {
            "action": None, "target": None, "status": "unresolved",
            "phase": "static", "apply_by": "adapter",
            "reason": "not_an_action", "why": "recipe entry is not an object",
        }
    name = action.get("action")
    capability = CAPABILITIES.get(name)
    target, why = resolution
    phase = PHASE_BY_ACTION.get(name, "manual" if capability == "manual" else "static")
    step = {
        "action": name,
        "target": action.get("target"),
        "status": capability or "unresolved",
        "phase": phase if capability == "ready" else (
            "terminal" if capability == "terminal" else "manual"),
        "apply_by": "human" if capability == "manual" else (
            "none" if capability == "terminal" else "adapter"),
    }
    _attach_target(step, target)

    if capability is None:
        step.update(status="unresolved", reason="unknown_action",
                    why="no capability is declared for this action")
        return step
    if capability == "terminal":
        step["reasons"] = action.get("reasons", [])
        return step
    if capability == "manual":
        step["hint"] = MANUAL_HINT.get(name, "no verified mcp_logic surface")
        step["reason"] = "manual"
        step["why"] = step["hint"]
        return step
    if target is None:
        step.update(status="unresolved", reason="unresolved_target",
                    why=why or "target could not be resolved")
        return step

    if name in ("fader", "pan"):
        if target["is_master"]:
            step.update(status="manual", apply_by="human", phase="manual",
                        reason="master_mixer_unverified",
                        hint=MASTER_FADER_HINT if name == "fader" else MASTER_PAN_HINT)
            step["why"] = step["hint"]
            return step
        if name == "fader":
            value = db_to_fader(action.get("gain_db", 0.0), fader_calibration)
            if value is None:
                step.update(
                    status="needs_calibration", reason="fader_calibration_required",
                    why=("the mixer takes a contract value with a measured taper, "
                         "not dB; run adapter.calibrate_fader on a scratch track "
                         "and pass --fader-calibration"))
                return step
            step["calls"] = [_mixer_call("set_volume", target, value)]
            return step
        position = action.get("position", 0.0)
        if not _is_number(position):
            step.update(status="unresolved", reason="pan_position_invalid",
                        why="pan.position must be a number in -1..1")
            return step
        step["calls"] = [_mixer_call("set_pan", target, float(position))]
        return step

    if name in EQ_ACTIONS:
        group = prepared.get(_group_key(target))
        if not group:
            step.update(status="unresolved", reason="eq_planning_failed",
                        why="internal: no EQ group was prepared for this target")
            return step
        override = group["override"].get(index)
        if override is not None:
            step.update(status=override["status"], reason=override["reason"],
                        why=override["why"])
            if override.get("hint"):
                step["hint"] = override["hint"]
            if override["status"] == "manual":
                step["apply_by"] = "human"
                step["phase"] = "manual"
            return step
        calls = group["calls"].get(index)
        if not calls:
            item = group["by_index"].get(index, {})
            step.update(status="unresolved",
                        reason=item.get("reason") or "eq_planning_failed",
                        why=item.get("why") or "no verified call could be built")
            return step
        step["calls"] = calls
        step["band"] = group["by_index"][index]["band"]
        step["insert"] = group["binding"]["insert"]
        warnings = group["warnings"].get(index)
        if warnings:
            step["warnings"] = warnings
        return step

    step.update(status="unresolved", reason="not_implemented",
                why="this action is marked ready but has no verified builder")
    return step


def _attach_target(step: dict, target: dict | None) -> None:
    if target is None:
        return
    step["target_key"] = _group_key(target)
    step["target_selector"] = _selector(target)
    step["track_name"] = target.get("name") or None


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
def summary(steps: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    calls = [call for step in steps for call in step.get("calls", [])]
    return {
        "total": len(steps),
        "by_status": counts,
        "calls": len(calls),
        "inserts": sum(1 for call in calls if call.get("kind") == "insert"),
    }


# ---------------------------------------------------------------------------
# apply with Honest Contract gating
# ---------------------------------------------------------------------------
def apply_steps(steps: list[dict], endpoint: str = "http://127.0.0.1:8765/mcp",
                timeout: float = 60, *, client=None,
                continue_on_error: bool = False, phases=None,
                bindings: dict | None = None,
                refresh_attempts: int = REFRESH_ATTEMPTS,
                refresh_delay: float = REFRESH_DELAY) -> list[dict]:
    """Execute planned calls, stopping unless every call reaches State A.

    Bindings are keyed by ``target_key`` so a caller may apply static steps and
    then bell steps in separate calls while reusing the insert acquired by the
    first.  No call is retried.  State C with ``write_attempted: true`` is
    reported as partial and always stops the chain.
    """
    bindings = bindings if bindings is not None else {}
    results: list[dict] = []
    owning = client is None
    if owning:
        try:
            client = mcp_client.connect(endpoint, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - report, never raise per call
            return _connection_failure(steps, exc, phases)

    target_bands = _target_bands(steps, phases)
    eq_state_checked: set[str] = set()
    skipped_targets: set[str] = set()
    stop = False
    try:
        for index, step in _ordered(steps, phases):
            if stop:
                break
            calls = step.get("calls") or []
            if not calls:
                continue
            for number, call in enumerate(calls):
                target_key = call.get("target_key")
                if target_key and target_key in skipped_targets:
                    results.append(_skipped_dependency(index, step, number, call))
                    continue
                if call.get("requires_insert"):
                    binding = bindings.get(target_key)
                    if not binding or not binding.get("ready"):
                        results.append(_dependency_failure(
                            index, step, number, call,
                            "requires the Channel EQ insert; no verified binding"))
                        skipped_targets.add(target_key)
                        if not continue_on_error:
                            stop = True
                        break

                prepared = _prepare_call(call)
                if prepared is None:
                    results.append(_contract_failure(index, step, number, call))
                    if _is_eq_call(call):
                        skipped_targets.add(target_key)
                    if not continue_on_error:
                        stop = True
                    break

                if _is_eq_write(call) and not _is_master_target(call):
                    binding = bindings.get(target_key)
                    ref = ((binding or {}).get("ref")
                           or prepared["arguments"]["params"].get("target_ref"))
                    if not ref:
                        results.append(_dependency_failure(
                            index, step, number, call,
                            "track Channel EQ writes need a plugin_insert_ref"))
                        skipped_targets.add(target_key)
                        if not continue_on_error:
                            stop = True
                        break
                    if target_key not in eq_state_checked:
                        check = _check_eq_enablement(
                            client, index, step, target_key, ref,
                            target_bands.get(target_key, ()), timeout)
                        if check is not None:
                            results.append(check)
                            skipped_targets.add(target_key)
                            if not continue_on_error:
                                stop = True
                            break
                        eq_state_checked.add(target_key)

                if binding := bindings.get(target_key):
                    prepared = _rebind(prepared, binding)

                result = _send(client, prepared, timeout)
                entry = _entry(index, step, number, call, result)
                results.append(entry)

                if result.applied:
                    if call.get("kind") == "insert":
                        binding = {"insert": call.get("insert"), "ref": None,
                                   "ready": False, "is_master": _is_master_target(call)}
                        refreshed, issue, chain = _refresh_after_insert(
                            client, call, result.payload, timeout,
                            refresh_attempts, refresh_delay)
                        if issue is not None:
                            results.append(_binding_failure(
                                index, step, number, call, issue, chain))
                            binding["failed"] = issue
                            bindings[target_key] = binding
                            skipped_targets.add(target_key)
                            if not continue_on_error:
                                stop = True
                            break
                        binding.update(refreshed)
                        binding["ready"] = True
                        bindings[target_key] = binding
                    continue

                if entry["status"] == "partial":
                    # The mutation may have landed; never continue the chain.
                    skipped_targets.add(target_key)
                    stop = True
                    break
                if _is_eq_call(call):
                    skipped_targets.add(target_key)
                if not continue_on_error:
                    stop = True
                    break
    finally:
        if owning:
            client.close()
    return results


def _ordered(steps, phases):
    selected = []
    for index, step in enumerate(steps):
        if phases is not None and step.get("phase") not in phases:
            continue
        selected.append((index, step))
    selected.sort(key=lambda item: (
        _PHASE_ORDER.get(item[1].get("phase"), 2), item[0]))
    return selected


def _connection_failure(steps, exc, phases) -> list[dict]:
    results = []
    for index, step in _ordered(steps, phases):
        calls = step.get("calls") or []
        if not calls:
            continue
        call = calls[0]
        results.append({
            "step": index, "call": 0, "kind": "transport",
            "operation": _operation(call), "status": "error",
            "state": None, "verified": None, "success": None,
            "hc_schema": None, "reason": None,
            "error": "bridge unavailable: %s" % exc,
            "write_attempted": False, "payload": {},
            "index": index, "action": step.get("action"),
            "detail": "bridge unavailable: %s" % exc,
            "target_key": call.get("target_key"),
        })
    return results


def _prepare_call(call: dict) -> dict | None:
    prepared = {
        "tool": call["tool"],
        "arguments": {
            "command": call["arguments"]["command"],
            "params": dict(call["arguments"]["params"]),
        },
    }
    try:
        contract.validate_call(prepared)
    except contract.ContractViolation:
        return None
    return prepared


def _rebind(prepared: dict, binding: dict) -> dict:
    params = prepared["arguments"]["params"]
    ref = binding.get("ref")
    if ref and "track" in params:
        params.pop("track", None)
        params["target_ref"] = ref
    if binding.get("insert") is not None:
        params["insert"] = binding["insert"]
    return prepared


def _send(client, call: dict, timeout: float):
    try:
        return client.call_tool(call["tool"], call["arguments"], timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - transport errors become data
        return mcp_client.ToolResult(ok=False, error=str(exc),
                                     tool=call["tool"],
                                     command=call["arguments"].get("command"))


def _entry(step_index: int, step: dict, number: int, call: dict, result) -> dict:
    payload = result.payload if isinstance(result.payload, dict) else {}
    status = "applied" if result.applied else (
        "partial" if payload.get("write_attempted") is True else "error")
    error = result.error
    if status == "error" and not error and not result.ok:
        error = "transport or JSON-RPC failure"
    return {
        "step": step_index, "call": number, "kind": call.get("kind"),
        "operation": _operation(call),
        "target_key": call.get("target_key"),
        "insert": call.get("insert"),
        "band": call.get("band"), "status": status,
        "state": result.state, "verified": result.verified,
        "success": result.success, "hc_schema": result.hc_schema,
        "reason": result.reason, "error": error,
        "write_attempted": bool(payload.get("write_attempted", True)),
        "payload": payload,
        # compatibility keys
        "index": step_index, "action": step.get("action"),
        "detail": error or result.reason or result.describe(),
    }


def _operation(call: dict) -> str:
    return "%s.%s" % (call["tool"], call["arguments"].get("command"))


def _is_eq_call(call: dict) -> bool:
    return call.get("tool") == contract.TOOL_PLUGINS


def _is_eq_write(call: dict) -> bool:
    return (call.get("tool") == contract.TOOL_PLUGINS
            and call["arguments"].get("command") == "set_eq_band_verified")


def _is_master_target(call: dict) -> bool:
    return call["arguments"]["params"].get("strip") == contract.MASTER_STRIP


def _target_bands(steps: list[dict], phases=None) -> dict[str, list[str]]:
    bands: dict[str, list[str]] = {}
    for step in steps:
        if phases is not None and step.get("phase") not in phases:
            continue
        for call in step.get("calls") or []:
            if not _is_eq_write(call):
                continue
            key = call.get("target_key") or ""
            band = call.get("band")
            if band and call["arguments"]["params"].get("strip") != contract.MASTER_STRIP:
                bands.setdefault(key, [])
                if band not in bands[key]:
                    bands[key].append(band)
    return bands


def _check_eq_enablement(client, index: int, step: dict, target_key: str,
                         ref: str, bands, timeout: float):
    call = {
        "tool": contract.TOOL_PLUGINS,
        "arguments": {"command": "get_channel_eq_state_verified",
                      "params": {"target_ref": ref}},
    }
    prepared = _prepare_call(call)
    if prepared is None:
        return _read_failure(index, step, target_key, call,
                             "channel EQ state read is outside the pinned contract")
    result = _send(client, prepared, timeout)
    if not result.applied:
        return _read_failure(
            index, step, target_key, call,
            "channel EQ state did not reach State A: %s" % result.describe(),
            result)
    payload = result.payload if isinstance(result.payload, dict) else {}
    if payload.get("complete") is not True:
        return _read_failure(
            index, step, target_key, call,
            "channel EQ state is incomplete: %s"
            % (payload.get("partial_reasons") or payload.get("partial_reason")
               or "unknown"), result)
    observed = {}
    for item in payload.get("bands") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            observed[item["name"]] = item
    for band in bands:
        item = observed.get(band)
        if item is None:
            return _read_failure(
                index, step, target_key, call,
                "channel EQ state does not report band %r" % band, result)
        enabled = item.get("enabled")
        if not isinstance(enabled, dict) or enabled.get("read_status") != "read" \
                or not isinstance(enabled.get("observed_raw"), bool):
            return _read_failure(
                index, step, target_key, call,
                "band %r enablement is unreadable" % band, result)
        if enabled["observed_raw"] is not True:
            return _read_failure(
                index, step, target_key, call,
                "band %r is disabled; a verified write cannot enable it" % band,
                result)
    return None


def _read_failure(index, step, target_key, call, message, result=None) -> dict:
    entry = {
        "step": index, "call": None, "kind": "read",
        "operation": _operation(call), "target_key": target_key,
        "state": getattr(result, "state", None),
        "verified": getattr(result, "verified", None),
        "success": getattr(result, "success", None),
        "hc_schema": getattr(result, "hc_schema", None),
        "reason": getattr(result, "reason", None),
        "error": message,
        "write_attempted": False,
        "payload": getattr(result, "payload", {}) if result is not None else {},
        "index": index, "action": step.get("action"), "detail": message,
        "status": "error", "reason_code": "band_enablement_unverified",
    }
    return entry


def _refresh_after_insert(client, call: dict, payload: dict, timeout: float,
                          attempts: int, delay: float):
    selector = _call_selector(call)
    key = call.get("target_key") or ""
    insert = call.get("insert")
    payload = payload if isinstance(payload, dict) else {}
    observed_slot = payload.get("observed_slot")
    if isinstance(observed_slot, int) and not isinstance(observed_slot, bool) \
            and observed_slot != insert:
        return None, "observed_slot_mismatch:%r" % observed_slot, None
    observed_plugin = payload.get("observed_plugin_id")
    if isinstance(observed_plugin, str) and observed_plugin != CHANNEL_EQ_PLUGIN_ID:
        return None, "observed_plugin_mismatch:%s" % observed_plugin, None
    chain = None
    issue = "inventory refresh did not run"
    for attempt in range(max(1, int(attempts))):
        try:
            result = client.get_inventory(selector)
            chain = from_tool_result(result, key=key, selector=selector)
        except Exception as exc:  # noqa: BLE001
            chain = InventoryChain(key=key, selector=selector,
                                   issues=("transport:%s" % exc,))
        if chain.reliable:
            slot, problem = verify_insert(chain, insert, CHANNEL_EQ_PLUGIN_ID)
            if problem is None:
                if chain.is_master:
                    return {"insert": insert, "ref": None}, None, chain
                if not slot.has_ref:
                    return None, "missing_plugin_insert_ref", chain
                return ({"insert": insert,
                         "ref": slot.plugin_insert_ref}, None, chain)
            issue = "insert_binding_failed:%s" % problem
        else:
            issue = "inventory_not_reliable:%s" % (
                ", ".join(chain.issues) or chain.state or "unavailable")
        if attempt + 1 < max(1, int(attempts)) and delay > 0:
            time.sleep(delay)
    return None, issue, chain


def _call_selector(call: dict) -> dict:
    params = call["arguments"]["params"]
    if params.get("strip") == contract.MASTER_STRIP:
        return dict(contract.MASTER_SELECTOR)
    return {"track": params["track"]}


def _binding_failure(index, step, number, call, issue, chain) -> dict:
    return {
        "step": index, "call": number, "kind": "binding",
        "operation": "logic_plugins.get_inventory",
        "target_key": call.get("target_key"),
        "state": getattr(chain, "state", None) if chain else None,
        "verified": None, "success": None, "hc_schema": None,
        "reason": issue, "error": issue,
        "write_attempted": False,
        "payload": chain.to_dict() if chain is not None else {},
        "index": index, "action": step.get("action"),
        "detail": issue, "status": "error",
        "reason_code": "insert_binding_failed",
    }


def _skipped_dependency(index, step, number, call) -> dict:
    message = "skipped: a previous call for this target did not reach State A"
    return {
        "step": index, "call": number, "kind": call.get("kind"),
        "operation": _operation(call), "target_key": call.get("target_key"),
        "state": None, "verified": False, "success": None, "hc_schema": None,
        "reason": "dependency_skipped", "error": None,
        "write_attempted": False, "payload": {},
        "index": index, "action": step.get("action"), "detail": message,
        "status": "skipped_dependency", "reason_code": "dependency_skipped",
    }


def _dependency_failure(index, step, number, call, message) -> dict:
    entry = _skipped_dependency(index, step, number, call)
    entry.update(status="skipped_dependency", detail=message,
                 reason="dependency_skipped", error=None)
    return entry


def _contract_failure(index, step, number, call) -> dict:
    message = "generated call is outside the pinned mcp_logic contract"
    return {
        "step": index, "call": number, "kind": call.get("kind"),
        "operation": _operation(call), "target_key": call.get("target_key"),
        "state": None, "verified": False, "success": None, "hc_schema": None,
        "reason": "contract_violation", "error": message,
        "write_attempted": False, "payload": {},
        "index": index, "action": step.get("action"), "detail": message,
        "status": "error", "reason_code": "contract_violation",
    }


def apply(steps: list[dict], endpoint: str = "http://127.0.0.1:8765/mcp",
          timeout: float = 60, *, client=None,
          continue_on_error: bool = False, bindings: dict | None = None,
          refresh_attempts: int = REFRESH_ATTEMPTS,
          refresh_delay: float = REFRESH_DELAY) -> list[dict]:
    """Compatibility wrapper: apply results plus one entry per no-call step."""
    results = apply_steps(
        steps, endpoint, timeout, client=client,
        continue_on_error=continue_on_error, bindings=bindings,
        refresh_attempts=refresh_attempts, refresh_delay=refresh_delay)
    executed = {result["step"] for result in results}
    for index, step in enumerate(steps):
        if step.get("calls") or index in executed:
            continue
        results.append({
            "step": index, "call": None, "status": "skipped",
            "operation": None, "state": None, "verified": None,
            "success": None, "hc_schema": None,
            "reason": step.get("reason"), "error": None,
            "write_attempted": False, "payload": {},
            "skipped": True,
            "index": index, "action": step.get("action"),
            "detail": step.get("hint") or step.get("why") or step["status"],
            "target_key": step.get("target_key"),
        })
    results.sort(key=lambda item: (item["step"],
                                    -1 if item.get("call") is None
                                    else item["call"]))
    return results


def step_as_dict(step: dict) -> dict:
    """Stable copy for reports/tests (never aliases planner state)."""
    return {key: value for key, value in step.items()}
