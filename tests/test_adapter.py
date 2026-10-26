"""Adapter tests: honest capabilities, inventory addressing and State A gating.

These tests are deliberately narrow.  They pin the *emitted* surface to what
mcp_logic HEAD b42cdd37 verifies, and they fail if the adapter starts writing
through an invented slot, a master Arrange index, a forbidden parameter name or
an unverified plugin.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adapter import mcp_logic_contract as contract  # noqa: E402
from adapter.cli import _parse_master, _parse_targets, main  # noqa: E402
from adapter.fader_calibration import FaderCalibration  # noqa: E402
from adapter.inventory import parse_inventory  # noqa: E402
from adapter.logic_adapter import (  # noqa: E402
    CAPABILITIES,
    MODE,
    RecipeError,
    apply,
    apply_steps,
    build_plan,
    db_to_fader,
    inventory_targets,
    plan,
    summary,
)
from adapter.mcp_client import ToolResult  # noqa: E402
from server import rules  # noqa: E402

PROJECT = "/tmp/session.logicx"
EQ_REF = "ins_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
COMP_REF = "ins_cccccccccccccccccccccccccccccccc"
SCHEMA = rules.RECIPE_SCHEMA_VERSION
CALIBRATION = FaderCalibration([[-18.0, 0.1], [-12.0, 0.2], [-6.0, 0.5], [0.0, 0.8]])


# ---------------------------------------------------------------------------
# fixtures mirroring the real inventory payloads
# ---------------------------------------------------------------------------
def _slot(insert, status, *, occupied=None, name=None, plugin_id=None,
          bypassed=None, ref=None):
    if occupied is None:
        occupied = status == "ok"
    item = {"insert": insert, "read_status": status, "occupied": occupied,
            "name": name, "plugin_id": plugin_id, "bypassed": bypassed}
    if ref is not None:
        item["plugin_insert_ref"] = ref
    return item


def _eq_slot(insert=0, ref=EQ_REF):
    return _slot(insert, "ok", name="Channel EQ",
                 plugin_id=contract.CHANNEL_EQ_PLUGIN_ID, bypassed=False, ref=ref)


def _compressor_slot(insert=2, ref=COMP_REF):
    return _slot(insert, "ok", name="Compressor",
                 plugin_id=contract.COMPRESSOR_PLUGIN_ID, bypassed=False, ref=ref)


def _track_payload(track=3, *, plugins=None, complete=True, refs=True):
    if plugins is None:
        plugins = [_eq_slot(0, EQ_REF if refs else None), _slot(1, "empty")]
    return {
        "success": True, "verified": True, "state": "A", "hc_schema": 2,
        "operation": "logic_plugins.get_inventory",
        "track": track, "strip": None, "complete": complete,
        "track_name": "Lead Vocal", "mixer_strip_index": track + 1,
        "plugins": plugins,
    }


def _master_payload(*, plugins=None, complete=True):
    if plugins is None:
        plugins = [_slot(1, "empty")]
    return {
        "success": True, "verified": True, "state": "A", "hc_schema": 2,
        "operation": "logic_plugins.get_inventory",
        "track": None, "strip": "output", "complete": complete,
        "track_name": "Stereo Out", "mixer_strip_index": 0,
        "plugins": plugins,
    }


def _chain(payload, key, selector):
    return parse_inventory(payload, key=key, selector=selector)


def _track_chain(payload=None, track=3):
    return _chain(payload or _track_payload(track), "track:%d" % track,
                  {"track": track})


def _master_chain(payload=None):
    return _chain(payload or _master_payload(), "master",
                  dict(contract.MASTER_SELECTOR))


def _action(name, target="vocal", **fields):
    action = {"action": name, "target": target, "schema": SCHEMA}
    action.update(fields)
    return action


def _bell(target="vocal", freq=2000, gain=-2.0, q=1.5, **fields):
    return _action("eq.bell", target, freq_hz=freq, gain_db=gain, q=q, **fields)


def _shelf(target="vocal", band="Low Shelf", freq=120, gain=-2.5, **fields):
    return _action("eq.shelf", target, band_name=band, freq_hz=freq,
                   gain_db=gain, **fields)


def _vocal(index=3, name="Lead Vocal"):
    return {"vocal": {"index": index, "name": name}}


def _calls(steps):
    return [call for step in steps for call in step.get("calls", [])]


def _operations(steps):
    return [call["arguments"]["command"] for call in _calls(steps)]


def _raises(exc_type, fn, needle=None):
    try:
        fn()
    except exc_type as exc:
        if needle is not None:
            assert needle in str(exc), (needle, str(exc))
        return exc
    raise AssertionError("expected %s" % exc_type.__name__)


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------
def test_capability_matrix_is_honest():
    assert contract.READY_ACTIONS == frozenset(
        {"fader", "pan", "eq.bell", "eq.shelf"})
    assert CAPABILITIES["eq.bell"] == "ready"
    assert CAPABILITIES["eq.shelf"] == "ready"
    assert CAPABILITIES["fader"] == "ready" and CAPABILITIES["pan"] == "ready"
    for name in ("limiter", "compressor", "polarity.invert", "eq.high_pass",
                 "eq.high_pass_stem", "de_esser", "dither", "stereo.width",
                 "dynamic_eq"):
        assert CAPABILITIES[name] == "manual", name
    assert CAPABILITIES["send_back"] == "terminal"
    # Every recipe action is declared; a new rules.ACTIONS entry cannot silently
    # become automatable.
    assert set(CAPABILITIES) >= set(rules.ACTIONS)


def test_manual_actions_emit_no_calls_and_explain_why():
    recipe = [_action("limiter", "master", ceiling_dbtp=-1.0,
                      input_gain_db=0.0, expected_gain_reduction_db=1.0),
              _action("compressor", "vocal", gain_reduction_db=2.0),
              _action("polarity.invert", "vocal"),
              _action("eq.high_pass", "vocal", freq_hz=25),
              _action("de_esser", "vocal", freq_hz=6000),
              _action("dither", "master", bit_depth=24)]
    steps = build_plan(recipe, _vocal(), {"name": "Stereo Out"}, PROJECT)
    assert all(step["status"] == "manual" for step in steps)
    assert all(step["apply_by"] == "human" for step in steps)
    assert all(step["phase"] == "manual" for step in steps)
    assert _calls(steps) == []
    limiter = next(step for step in steps if step["action"] == "limiter")
    assert "not in mcp_logic's insert allowlist" in limiter["hint"]
    compressor = next(step for step in steps if step["action"] == "compressor")
    assert "ratio/attack/release" in compressor["hint"]


def test_send_back_is_terminal_and_emits_nothing():
    steps = build_plan([_action("send_back", "master", reasons=["mix_needs_work"])],
                       {}, {"name": "Stereo Out"}, PROJECT)
    assert steps[0]["status"] == "terminal"
    assert steps[0]["phase"] == "terminal"
    assert steps[0]["reasons"] == ["mix_needs_work"]
    assert _calls(steps) == []


def test_unknown_action_is_not_a_recipe():
    exc = _raises(RecipeError, lambda: build_plan(
        [_action("frobnicate", "master")], {}, {"name": "Stereo Out"}, PROJECT),
        "unknown_action")
    assert exc.violations[0]["code"] == "unknown_action"


# ---------------------------------------------------------------------------
# master addressing
# ---------------------------------------------------------------------------
def test_parse_master_accepts_a_bare_name_and_treats_index_as_informational():
    assert _parse_master("Stereo Out") == {"index": None, "name": "Stereo Out"}
    assert _parse_master("0:Stereo Out") == {"index": 0, "name": "Stereo Out"}
    assert _parse_master("master=0:Stereo Out") == {"index": 0, "name": "Stereo Out"}
    assert _parse_master("") is None


def test_master_bell_uses_strip_output_and_never_a_track_or_ref():
    steps = build_plan([_bell("master")], {}, {"index": 0, "name": "Stereo Out"},
                       PROJECT, inventory={"master": _master_chain(
                           _master_payload(plugins=[_slot(2, "empty")]))})
    step = steps[0]
    assert step["status"] == "ready"
    assert step["target_selector"] == {"strip": "output"}
    assert step["warnings"] == ["band_enablement_unverified"]
    insert, *writes = step["calls"]
    assert insert["arguments"]["command"] == "insert_verified"
    params = insert["arguments"]["params"]
    assert params["strip"] == "output"
    assert params["insert"] == 2  # from inventory, not a hard-coded 0
    assert params["expected_name"] == "Stereo Out"
    assert "track" not in params and "target_ref" not in params
    for write in writes:
        values = write["arguments"]["params"]
        assert values["strip"] == "output" and values["insert"] == 2
        assert "track" not in values and "target_ref" not in values
        assert "expected_name" not in values


def test_master_existing_eq_is_manual_never_reused():
    steps = build_plan([_bell("master")], {}, {"name": "Stereo Out"}, PROJECT,
                       inventory={"master": _master_chain(
                           _master_payload(plugins=[_eq_slot(0, None)]))})
    step = steps[0]
    assert step["status"] == "manual"
    assert step["reason"] == "master_eq_state_unreadable"
    assert step["apply_by"] == "human"
    assert "calls" not in step or step["calls"] == []


def test_mix_bus_is_never_mapped_to_the_master_strip():
    recipe = [_action("pan", "mix_bus", position=0.5)]
    unresolved = build_plan(recipe, {}, {"name": "Stereo Out"}, PROJECT)
    assert unresolved[0]["status"] == "unresolved"
    assert "never mapped to the master" in unresolved[0]["why"]
    assert _calls(unresolved) == []

    mapped = build_plan(recipe, {"mix_bus": {"index": 5, "name": "Music Bus"}},
                        {"name": "Stereo Out"}, PROJECT)
    assert mapped[0]["status"] == "ready"
    assert mapped[0]["target_selector"] == {"track": 5}
    assert mapped[0]["calls"][0]["arguments"]["params"] == \
        {"track": 5, "value": 0.5}


def test_master_fader_and_pan_are_manual():
    steps = build_plan([_action("fader", "master", gain_db=-1.0),
                        _action("pan", "master", position=0.0)],
                       {}, {"name": "Stereo Out"}, PROJECT)
    for step in steps:
        assert step["status"] == "manual"
        assert step["reason"] == "master_mixer_unverified"
        assert step["apply_by"] == "human"
    assert _calls(steps) == []


# ---------------------------------------------------------------------------
# track addressing, slots and overwrite rules
# ---------------------------------------------------------------------------
def test_new_eq_uses_the_first_verified_empty_slot():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_compressor_slot(0), _slot(1, "empty"),
                                _slot(2, "empty")]))}
    steps = build_plan([_bell()], _vocal(), None, PROJECT, inventory=inventory)
    step = steps[0]
    assert step["status"] == "ready"
    assert step["insert"] == 1
    insert = step["calls"][0]
    params = insert["arguments"]["params"]
    assert params["track"] == 3
    assert params["expected_name"] == "Lead Vocal"
    assert params["insert"] == 1
    assert "target_ref" not in params


def test_no_free_slot_is_unresolved():
    inventory = {"track:3": _track_chain(_track_payload(plugins=[
        _compressor_slot(0, COMP_REF),
        _compressor_slot(1, "ins_dddddddddddddddddddddddddddddddd")]))}
    step = build_plan([_bell()], _vocal(), None, PROJECT,
                      inventory=inventory)[0]
    assert step["status"] == "unresolved"
    assert step["reason"] == "no_free_insert_slot"
    assert "calls" not in step


def test_existing_track_eq_requires_overwrite_and_uses_its_ref():
    inventory = {"track:3": _track_chain()}
    blocked = build_plan([_bell()], _vocal(), None, PROJECT,
                         inventory=inventory)[0]
    assert blocked["status"] == "unresolved"
    assert blocked["reason"] == "eq_overwrite_required"

    ready = build_plan([_bell()], _vocal(), None, PROJECT,
                       inventory=inventory, overwrite_eq=True)[0]
    assert ready["status"] == "ready"
    assert ready["insert"] == 0
    assert ready["warnings"] == ["band_enablement_unverified"]
    assert [call["arguments"]["command"] for call in ready["calls"]] == \
        ["set_eq_band_verified"] * 3
    for call in ready["calls"]:
        params = call["arguments"]["params"]
        assert params["target_ref"] == EQ_REF
        assert "track" not in params
        assert params["insert"] == 0


def test_existing_track_eq_without_a_ref_needs_inventory():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_eq_slot(0, None), _slot(1, "empty")]))}
    step = build_plan([_bell()], _vocal(), None, PROJECT,
                      inventory=inventory, overwrite_eq=True)[0]
    assert step["status"] == "needs_inventory"
    assert step["reason"] == "missing_plugin_insert_ref"
    assert "calls" not in step


def test_duplicate_or_bypassed_eq_is_unresolved():
    duplicate = _track_payload(plugins=[_eq_slot(0), _eq_slot(2)])
    step = build_plan([_bell()], _vocal(), None, PROJECT,
                      inventory={"track:3": _track_chain(duplicate)},
                      overwrite_eq=True)[0]
    assert step["status"] == "unresolved"
    assert step["reason"] == "duplicate_channel_eq"

    bypassed = _track_payload(plugins=[_slot(
        0, "ok", name="Channel EQ", plugin_id=contract.CHANNEL_EQ_PLUGIN_ID,
        bypassed=True, ref=EQ_REF)])
    step = build_plan([_bell()], _vocal(), None, PROJECT,
                      inventory={"track:3": _track_chain(bypassed)},
                      overwrite_eq=True)[0]
    assert step["status"] == "unresolved"
    assert step["reason"] == "plugin_bypassed_or_unknown"


def test_no_inventory_means_needs_inventory_not_an_invented_slot():
    step = build_plan([_bell()], _vocal(), None, PROJECT)[0]
    assert step["status"] == "needs_inventory"
    assert step["reason"] == "inventory_required"
    assert "calls" not in step


def test_project_and_track_name_are_required_before_inventory():
    no_project = build_plan([_bell()], _vocal(), None, None,
                            inventory={"track:3": _track_chain()})[0]
    assert no_project["status"] == "unresolved"
    assert no_project["reason"] == "project_required"

    unnamed = build_plan([_bell()], {"vocal": {"index": 3, "name": ""}},
                         None, PROJECT, inventory={"track:3": _track_chain()})[0]
    assert unnamed["status"] == "unresolved"
    assert unnamed["reason"] == "track_name_required"


def test_unreliable_inventory_never_authorises_writes():
    payload = _track_payload(complete=False, plugins=[
        _slot(0, "empty"), _slot(1, "unreadable", occupied=True)])
    step = build_plan([_bell()], _vocal(), None, PROJECT,
                      inventory={"track:3": _track_chain(payload)})[0]
    assert step["status"] == "needs_inventory"
    assert step["reason"] == "inventory_not_reliable"
    assert "calls" not in step


# ---------------------------------------------------------------------------
# bands and parameter names
# ---------------------------------------------------------------------------
def test_two_bells_get_distinct_peak_bands():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_slot(1, "empty")]))}
    steps = build_plan([_bell(freq=200), _bell(freq=3000)], _vocal(), None,
                       PROJECT, inventory=inventory)
    assert [step["band"] for step in steps] == ["Peak 1", "Peak 2"]
    writes = [call for call in _calls(steps) if call["kind"] == "write"]
    assert {call["band"] for call in writes} == {"Peak 1", "Peak 2"}
    # One insert, assigned to the first EQ step in phase/index order.
    inserts = [call for call in _calls(steps) if call["kind"] == "insert"]
    assert len(inserts) == 1
    assert inserts[0] in steps[0]["calls"]
    assert all(call["requires_insert"] for call in steps[1]["calls"])


def test_explicit_band_collision_and_unverified_band_are_unresolved():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_slot(1, "empty")]))}
    collided = build_plan(
        [_bell(band_name="Peak 1"), _bell(band_name="Peak 1")],
        _vocal(), None, PROJECT, inventory=inventory)
    assert [step["reason"] for step in collided] == ["band_collision"] * 2
    assert _calls(collided) == []

    unverified = build_plan([_bell(band_name="Low Cut")], _vocal(), None,
                            PROJECT, inventory=inventory)[0]
    assert unverified["status"] == "unresolved"
    assert unverified["reason"] == "band_not_verified"


def test_more_than_four_bells_does_not_wrap_around():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_slot(1, "empty")]))}
    steps = build_plan([_bell(freq=100 * (index + 1)) for index in range(5)],
                       _vocal(), None, PROJECT, inventory=inventory)
    assert [step["band"] for step in steps[:4]] == \
        ["Peak 1", "Peak 2", "Peak 3", "Peak 4"]
    assert steps[4]["status"] == "unresolved"
    assert steps[4]["reason"] == "no_free_band"


def test_shelf_requires_an_explicit_side_and_preserves_sign_and_q():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_slot(1, "empty")]))}
    no_side = build_plan([_shelf(band=None)], _vocal(), None, PROJECT,
                         inventory=inventory)[0]
    assert no_side["status"] == "unresolved"
    assert no_side["reason"] == "shelf_band_required"

    step = build_plan([_shelf(band="Low Shelf", gain=-2.5, q=0.7)],
                      _vocal(), None, PROJECT, inventory=inventory)[0]
    assert step["status"] == "ready"
    insert, *writes = step["calls"]
    assert insert["arguments"]["command"] == "insert_verified"
    by_parameter = {call["arguments"]["params"]["parameter"]:
                    call["arguments"]["params"] for call in writes}
    assert by_parameter["Frequency"] == dict(
        by_parameter["Frequency"], value=120.0, unit="Hz")
    assert by_parameter["Gain"]["value"] == -2.5
    assert by_parameter["Gain"]["unit"] == "dB"
    assert by_parameter["Q"]["value"] == 0.7
    assert by_parameter["Q"]["unit"] == "Q"


def test_q_is_literal_q_and_never_q_factor():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_slot(1, "empty")]))}
    step = build_plan([_bell(q=2.0)], _vocal(), None, PROJECT,
                      inventory=inventory)[0]
    params = [call["arguments"]["params"] for call in step["calls"]
              if call["kind"] == "write"]
    assert any(item["parameter"] == "Q" and item["unit"] == "Q" for item in params)
    assert all("Q-Factor" not in json.dumps(call) for call in step["calls"])


def test_insert_is_on_the_first_eq_step_in_phase_order():
    inventory = {"track:3": _track_chain(
        _track_payload(plugins=[_slot(1, "empty")]))}
    steps = build_plan([_bell(), _shelf()], _vocal(), None, PROJECT,
                       inventory=inventory)
    bell, shelf = steps
    assert bell["phase"] == "bell" and shelf["phase"] == "static"
    assert shelf["calls"][0]["kind"] == "insert"
    assert bell["calls"][0]["kind"] == "write"
    assert len([call for call in _calls(steps) if call["kind"] == "insert"]) == 1
    assert all(call["requires_insert"] for call in bell["calls"])


# ---------------------------------------------------------------------------
# targets, faders and every emitted call
# ---------------------------------------------------------------------------
def test_fader_requires_a_measured_calibration():
    recipe = [_action("fader", "vocal", gain_db=-6.0)]
    missing = build_plan(recipe, _vocal(), None, PROJECT)[0]
    assert missing["status"] == "needs_calibration"
    assert "calls" not in missing

    ready = build_plan(recipe, _vocal(), None, PROJECT,
                       fader_calibration=CALIBRATION)[0]
    assert ready["status"] == "ready"
    assert ready["calls"][0]["arguments"]["params"] == \
        {"track": 3, "value": 0.5}
    assert db_to_fader(-6.0, CALIBRATION) == 0.5
    assert db_to_fader(-6.0, None) is None


def test_pan_is_track_bound_and_validated():
    ready = build_plan([_action("pan", "vocal", position=-0.25)], _vocal(),
                       None, PROJECT)[0]
    assert ready["status"] == "ready"
    assert ready["calls"][0]["arguments"] == {
        "command": "set_pan", "params": {"track": 3, "value": -0.25}}

    invalid = build_plan([_action("pan", "vocal", position="left")], _vocal(),
                         None, PROJECT)[0]
    assert invalid["status"] == "unresolved"
    assert invalid["reason"] == "pan_position_invalid"


def test_stem_resolution_uses_the_exported_basename():
    tracks = {"01_drums.wav": {"index": 4, "name": "Drums"}}
    resolved = build_plan(
        [_action("pan", "vocal", stem="/stems/01_drums.wav", position=0.1)],
        tracks, None, PROJECT)[0]
    assert resolved["status"] == "ready"
    assert resolved["target_selector"] == {"track": 4}

    missing = build_plan(
        [_action("pan", "stem", stem="/stems/99_unknown.wav", position=0.1)],
        tracks, None, PROJECT)[0]
    assert missing["status"] == "unresolved"
    assert "01_drums.wav" not in missing["why"]


def test_every_generated_call_passes_the_pinned_contract():
    recipe = [_bell(), _shelf(band="High Shelf", freq=9000, gain=1.5),
              _action("fader", "vocal", gain_db=-2.0),
              _action("pan", "vocal", position=0.2),
              _action("limiter", "master", ceiling_dbtp=-1.0,
                      input_gain_db=0.0, expected_gain_reduction_db=1.0)]
    steps = build_plan(recipe, _vocal(), {"name": "Stereo Out"}, PROJECT,
                       CALIBRATION,
                       inventory={"track:3": _track_chain(
                           _track_payload(plugins=[_slot(1, "empty")]))})
    calls = _calls(steps)
    assert calls
    for call in calls:
        contract.validate_call(call)
        assert contract.emitted_forbidden(call) is None
        assert "requires_insert" not in call["arguments"]
        assert "target_key" not in call["arguments"]


def test_each_target_gets_at_most_one_insert():
    recipe = [_bell("vocal"), _shelf("drums", band="High Shelf", freq=8000,
                                     gain=1.0)]
    tracks = {"vocal": {"index": 3, "name": "Lead Vocal"},
              "drums": {"index": 4, "name": "Drums"}}
    inventory = {"track:3": _track_chain(
                     _track_payload(track=3, plugins=[_slot(1, "empty")]),
                     track=3),
                 "track:4": _track_chain(
                     _track_payload(track=4, plugins=[_slot(2, "empty")]),
                     track=4)}
    steps = build_plan(recipe, tracks, None, PROJECT, inventory=inventory)
    inserts = [call for call in _calls(steps) if call["kind"] == "insert"]
    assert len(inserts) == 2
    assert {call["insert"] for call in inserts} == {1, 2}
    assert {call["target_key"] for call in inserts} == {"track:3", "track:4"}


def test_inventory_targets_resolves_like_the_planner():
    recipe = [_bell("vocal"), _shelf("master", band="High Shelf", freq=9000),
              _action("limiter", "master", ceiling_dbtp=-1.0,
                      input_gain_db=0.0, expected_gain_reduction_db=1.0)]
    targets = inventory_targets(recipe, _vocal(), {"name": "Stereo Out"})
    assert {target["target"]["key"] for target in targets} == \
        {"vocal", "master"}
    assert all("vocal" in target["keys"] for target in targets
               if target["target"]["key"] == "vocal")


def test_recipe_validation_rejects_missing_schema_and_nonterminal_send_back():
    no_schema = [{"action": "pan", "target": "vocal", "position": 0.0}]
    exc = _raises(RecipeError, lambda: build_plan(no_schema, _vocal(), None,
                                                  PROJECT),
                  "schema_mismatch")
    assert any(item["code"] == "schema_mismatch" for item in exc.violations)

    _raises(RecipeError, lambda: build_plan(
        [_action("send_back", "master", reasons=["x"]),
         _action("pan", "vocal", position=0.0)], _vocal(), None, PROJECT),
        "send_back_not_terminal")


def test_summary_counts_calls_and_inserts():
    steps = build_plan([_bell()], _vocal(), None, PROJECT,
                       inventory={"track:3": _track_chain(
                           _track_payload(plugins=[_slot(1, "empty")]))})
    report = summary(steps)
    assert report["total"] == 1
    assert report["by_status"] == {"ready": 1}
    assert report["calls"] == 4
    assert report["inserts"] == 1


# ---------------------------------------------------------------------------
# apply: Honest Contract gating
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self, results=None, inventories=None, eq_state=None):
        self.results = list(results or [])
        self.inventories = list(inventories or [])
        self.eq_state = eq_state
        self.calls = []
        self.inventory_reads = 0

    def call_tool(self, tool, arguments, timeout=None):
        self.calls.append((tool, dict(arguments)))
        if arguments.get("command") == "get_channel_eq_state_verified":
            assert self.eq_state is not None, "unexpected EQ state read"
            return self.eq_state
        if not self.results:
            raise AssertionError("unexpected call: %s" % (arguments,))
        return self.results.pop(0)

    def get_inventory(self, selector):
        self.inventory_reads += 1
        if not self.inventories:
            raise AssertionError("unexpected inventory read")
        return self.inventories.pop(0)

    def close(self):
        pass


def _ok(payload=None, *, tool=contract.TOOL_PLUGINS, hc_schema=2):
    data = {"success": True, "verified": True, "state": "A"}
    if tool in contract.HC_V2_TOOLS:
        data["hc_schema"] = hc_schema
    data.update(payload or {})
    return ToolResult(
        ok=True, success=True, verified=True, state="A",
        hc_schema=hc_schema if tool in contract.HC_V2_TOOLS else None,
        payload=data, tool=tool, command="command")


def _state_b(reason="readback_mismatch", *, tool=contract.TOOL_PLUGINS):
    data = {"success": True, "verified": False, "state": "B",
            "reason": reason}
    if tool in contract.HC_V2_TOOLS:
        data["hc_schema"] = 2
    return ToolResult(
        ok=True, success=True, verified=False, state="B", reason=reason,
        hc_schema=2 if tool in contract.HC_V2_TOOLS else None,
        payload=data, tool=tool)


def _state_c(error="write_failed", *, write_attempted, payload=None,
             tool=contract.TOOL_PLUGINS):
    data = {"success": False, "verified": False, "state": "C",
            "error": error, "write_attempted": write_attempted}
    if tool in contract.HC_V2_TOOLS:
        data["hc_schema"] = 2
    data.update(payload or {})
    return ToolResult(
        ok=True, success=False, verified=False, state="C", error=error,
        hc_schema=2 if tool in contract.HC_V2_TOOLS else None,
        payload=data, tool=tool)


def _transport_error(message="bridge down"):
    return ToolResult(ok=False, error=message, tool=contract.TOOL_PLUGINS)


def _eq_state(bands=("Peak 1",), *, enabled=True, complete=True):
    names = ["Low Cut", "Low Shelf", "Peak 1", "Peak 2", "Peak 3", "Peak 4",
             "High Shelf", "High Cut"]
    items = [{
        "name": name, "band": position + 1,
        "enabled": {
            "read_status": "read",
            "observed_raw": (enabled if name in bands else True),
        },
    } for position, name in enumerate(names)]
    payload = {"complete": complete, "snapshot_atomic": False, "bands": items}
    if not complete:
        payload["partial_reasons"] = ["band_state_unavailable"]
    return _ok(payload)


def _fader_steps(count=2):
    tracks = {"first": {"index": 1, "name": "First"},
              "second": {"index": 2, "name": "Second"}}
    recipe = [_action("fader", name, gain_db=-6.0)
              for name in ("first", "second")][:count]
    return plan(recipe, tracks, None, PROJECT, CALIBRATION)


def _bell_steps(target="vocal", track=3):
    tracks = {"vocal": {"index": track, "name": "Lead Vocal"}}
    inventory = {"track:%d" % track: _track_chain(
        _track_payload(track=track, plugins=[_slot(1, "empty")]), track)}
    return plan([_bell()], tracks, None, PROJECT, inventory=inventory)


def test_apply_state_a_is_applied():
    results = apply_steps(_fader_steps(1), client=FakeClient([_ok(
        {"write_attempted": True}, tool=contract.TOOL_MIXER)]))
    assert len(results) == 1
    assert results[0]["status"] == "applied"
    assert results[0]["state"] == "A"
    assert results[0]["verified"] is True
    assert results[0]["operation"] == "logic_mixer.set_volume"


def test_apply_state_b_stops_the_chain():
    client = FakeClient([_state_b(tool=contract.TOOL_MIXER)])
    results = apply_steps(_fader_steps(), client=client)
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert results[0]["verified"] is False
    assert results[0]["reason"] == "readback_mismatch"
    assert len(client.calls) == 1


def test_apply_transport_error_is_never_applied():
    client = FakeClient([_transport_error()])
    results = apply_steps(_fader_steps(), client=client)
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "bridge down" in results[0]["detail"]
    assert len(client.calls) == 1


def test_apply_write_attempted_state_c_is_partial_and_stops_even_with_continue():
    client = FakeClient([_state_c("readback_failed", write_attempted=True,
                                  tool=contract.TOOL_MIXER,
                                  payload={"safe_to_retry": False})])
    results = apply_steps(_fader_steps(), client=client, continue_on_error=True)
    assert len(results) == 1
    assert results[0]["status"] == "partial"
    assert results[0]["payload"]["safe_to_retry"] is False
    assert len(client.calls) == 1


def test_apply_insert_failure_blocks_parameter_writes():
    client = FakeClient([_state_c("slot_occupied", write_attempted=False)])
    results = apply_steps(_bell_steps(), client=client)
    assert results[0]["status"] == "error"
    assert not any(
        arguments["command"] == "set_eq_band_verified"
        for _tool, arguments in client.calls)
    assert len(results) == 1


def test_apply_continue_on_error_skips_only_dependent_calls():
    tracks = {"a": {"index": 3, "name": "A"}, "b": {"index": 4, "name": "B"}}
    steps = plan([_bell("a"), _bell("b")], tracks, None, PROJECT,
                 inventory={
                     "track:3": _track_chain(
                         _track_payload(plugins=[_slot(1, "empty")]), 3),
                     "track:4": _track_chain(
                         _track_payload(track=4,
                                        plugins=[_slot(1, "empty")]), 4),
                 })
    results = [item for item in steps if item["action"] == "eq.bell"]
    assert len(results) == 2
    track4_post_insert = _track_payload(
        track=4, plugins=[_eq_slot(1), _slot(2, "empty")])
    client = FakeClient(
        [_state_c("slot_occupied", write_attempted=False),
         _ok({"observed_slot": 1, "write_attempted": True,
              "observed_plugin_id": contract.CHANNEL_EQ_PLUGIN_ID}),
         _ok({"write_attempted": True}), _ok({"write_attempted": True}),
         _ok({"write_attempted": True})],
        inventories=[_ok(track4_post_insert)],
        eq_state=_eq_state(["Peak 1"]))
    applied = apply_steps(steps, client=client, continue_on_error=True,
                          refresh_delay=0)
    statuses = [item["status"] for item in applied]
    assert statuses.count("skipped_dependency") == 3
    assert statuses.count("applied") == 4
    writes = [arguments["params"] for _tool, arguments in client.calls
              if arguments["command"] == "set_eq_band_verified"]
    assert writes and all(params.get("target_ref") == EQ_REF for params in writes)
    assert all(params.get("track") in (None, 4) for params in writes)


def test_apply_rebinds_track_writes_after_insert_and_reads_band_state():
    client = FakeClient(
        [_ok({"observed_slot": 1, "write_attempted": True,
              "observed_plugin_id": contract.CHANNEL_EQ_PLUGIN_ID}),
         _ok({"write_attempted": True}), _ok({"write_attempted": True}),
         _ok({"write_attempted": True})],
        inventories=[_ok(_track_payload(plugins=[_eq_slot(1),
                                                 _slot(2, "empty")]))],
        eq_state=_eq_state(["Peak 1"]))
    results = apply_steps(_bell_steps(), client=client, refresh_delay=0)
    assert [item["status"] for item in results] == ["applied"] * 4
    commands = [arguments["command"] for _tool, arguments in client.calls]
    assert commands[0] == "insert_verified"
    assert commands[1] == "get_channel_eq_state_verified"
    assert commands[2:] == ["set_eq_band_verified"] * 3
    for _tool, arguments in client.calls[2:]:
        params = arguments["params"]
        assert params["target_ref"] == EQ_REF
        assert params["insert"] == 1
        assert "track" not in params


def test_apply_refresh_without_a_ref_writes_nothing():
    client = FakeClient(
        [_ok({"observed_slot": 1, "write_attempted": True})],
        inventories=[_ok(_track_payload(plugins=[_eq_slot(1, None)]))])
    results = apply_steps(_bell_steps(), client=client, refresh_delay=0)
    assert results[0]["status"] == "applied"
    assert results[1]["status"] == "error"
    assert results[1]["reason"] == "missing_plugin_insert_ref"
    assert not any(arguments["command"] == "set_eq_band_verified"
                   for _tool, arguments in client.calls)


def test_apply_refresh_mismatch_writes_nothing():
    client = FakeClient(
        [_ok({"observed_slot": 1, "write_attempted": True})],
        inventories=[_ok(_track_payload(plugins=[
            _slot(1, "empty"), _eq_slot(2)]))])
    results = apply_steps(_bell_steps(), client=client, refresh_delay=0)
    assert results[1]["status"] == "error"
    assert results[1]["reason_code"] == "insert_binding_failed"
    assert not any(arguments["command"] == "set_eq_band_verified"
                   for _tool, arguments in client.calls)


def test_apply_disabled_band_stops_before_writing():
    client = FakeClient(
        [_ok({"observed_slot": 1, "write_attempted": True})],
        inventories=[_ok(_track_payload(plugins=[_eq_slot(1),
                                                 _slot(2, "empty")]))],
        eq_state=_eq_state(["Peak 1"], enabled=False))
    results = apply_steps(_bell_steps(), client=client, refresh_delay=0)
    assert results[0]["status"] == "applied"
    assert results[1]["status"] == "error"
    assert results[1]["reason_code"] == "band_enablement_unverified"
    assert not any(arguments["command"] == "set_eq_band_verified"
                   for _tool, arguments in client.calls)


def test_apply_master_binds_by_strip_and_insert_without_a_state_read():
    inventory = {"master": _master_chain(
        _master_payload(plugins=[_slot(2, "empty")]))}
    steps = plan([_bell("master")], {}, {"name": "Stereo Out"}, PROJECT,
                 inventory=inventory)
    client = FakeClient(
        [_ok({"observed_slot": 2, "write_attempted": True,
              "observed_plugin_id": contract.CHANNEL_EQ_PLUGIN_ID}),
         _ok({"write_attempted": True}), _ok({"write_attempted": True}),
         _ok({"write_attempted": True})],
        inventories=[_ok(_master_payload(
            plugins=[_slot(0, "empty"), _slot(1, "empty"), _eq_slot(2, None)]))])
    results = apply_steps(steps, client=client, refresh_delay=0)
    assert [item["status"] for item in results] == ["applied"] * 4
    assert all(arguments["command"] != "get_channel_eq_state_verified"
               for _tool, arguments in client.calls)
    for _tool, arguments in client.calls[1:]:
        params = arguments["params"]
        assert params["strip"] == "output" and params["insert"] == 2
        assert "target_ref" not in params and "track" not in params


def test_apply_pre_existing_track_eq_reads_state_and_uses_its_ref():
    steps = plan([_bell()], _vocal(), None, PROJECT,
                 inventory={"track:3": _track_chain()}, overwrite_eq=True)
    client = FakeClient(
        [_ok({"write_attempted": True})] * 3,
        eq_state=_eq_state(["Peak 1"]))
    results = apply_steps(steps, client=client)
    assert [item["status"] for item in results] == ["applied"] * 3
    assert client.inventory_reads == 0
    commands = [arguments["command"] for _tool, arguments in client.calls]
    assert commands[0] == "get_channel_eq_state_verified"
    assert all("insert_verified" != command for command in commands)
    assert all(arguments["params"]["target_ref"] == EQ_REF
               for _tool, arguments in client.calls[1:])


def test_apply_rejects_a_call_outside_the_contract_without_sending_it():
    step = {
        "action": "limiter", "status": "ready", "phase": "static",
        "calls": [{
            "tool": contract.TOOL_PLUGINS,
            "arguments": {"command": "insert_verified", "params": {
                "track": 3, "insert": 0, "plugin": "Limiter",
                "expected_name": "Vox", "mode": MODE,
                "project_expected_path": PROJECT}},
            "kind": "insert", "requires_insert": False,
            "target_key": "track:3", "insert": 0,
        }],
    }
    client = FakeClient()
    results = apply_steps([step], client=client)
    assert results[0]["status"] == "error"
    assert results[0]["reason_code"] == "contract_violation"
    assert client.calls == []


def test_apply_connection_failure_is_reported_per_step():
    results = apply_steps(_fader_steps(1), endpoint="http://127.0.0.1:1/mcp",
                          timeout=0.5)
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert results[0]["kind"] == "transport"
    assert "bridge unavailable" in results[0]["error"]


def test_apply_wrapper_includes_manual_steps_as_skipped():
    recipe = [_action("limiter", "master", ceiling_dbtp=-1.0,
                      input_gain_db=0.0, expected_gain_reduction_db=1.0),
              _action("fader", "vocal", gain_db=-6.0)]
    steps = plan(recipe, _vocal(), {"name": "Stereo Out"}, PROJECT,
                 CALIBRATION)
    results = apply(steps, client=FakeClient(
        [_ok({"write_attempted": True}, tool=contract.TOOL_MIXER)]))
    assert [item["status"] for item in results] == ["skipped", "applied"]
    assert results[0]["action"] == "limiter"
    assert results[1]["action"] == "fader"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def test_parse_targets_are_strict():
    assert _parse_targets("vocal=3:Lead Vocal,01_bass.wav=2:Bass") == {
        "vocal": {"index": 3, "name": "Lead Vocal"},
        "01_bass.wav": {"index": 2, "name": "Bass"},
    }
    for text in ("vocal", "vocal:3", "vocal=x:Name"):
        _raises(ValueError, lambda: _parse_targets(text))


def _run_main(argv):
    output = io.StringIO()
    previous = sys.argv
    sys.argv = ["adapter"] + argv
    try:
        with contextlib.redirect_stdout(output):
            code = main()
    finally:
        sys.argv = previous
    return code, output.getvalue()


def test_cli_dry_run_plans_from_an_inventory_file_without_writing():
    recipe = [
        {"action": "eq.bell", "target": "vocal", "schema": SCHEMA,
         "freq_hz": 2000, "gain_db": -2.0, "q": 1.5},
        {"action": "eq.shelf", "target": "master", "schema": SCHEMA,
         "band_name": "High Shelf", "freq_hz": 9000, "gain_db": 1.0},
        {"action": "eq.high_pass", "target": "vocal", "schema": SCHEMA,
         "freq_hz": 25},
    ]
    inventory = {
        "vocal": _track_payload(plugins=[_slot(1, "empty")]),
        "master": _master_payload(plugins=[_slot(2, "empty")]),
    }
    paths = _write_temp({"recipe.json": recipe, "inventory.json": inventory})
    try:
        code, output = _run_main([
            "--recipe", paths["recipe.json"], "--project", PROJECT,
            "--tracks", "vocal=3:Lead Vocal", "--master", "Stereo Out",
            "--inventory", paths["inventory.json"]])
    finally:
        _cleanup(paths)
    assert code == 0
    report = json.loads(output)
    assert report["dry_run"] is True
    assert report["summary"]["by_status"] == {"ready": 2, "manual": 1}
    vocal, master, high_pass = report["steps"]
    assert vocal["calls"][0]["arguments"]["params"]["insert"] == 1
    master_insert = master["calls"][0]["arguments"]["params"]
    assert master_insert["strip"] == "output"
    assert master_insert["insert"] == 2
    assert master_insert["expected_name"] == "Stereo Out"
    assert "calls" not in high_pass or high_pass["calls"] == []


def test_cli_refuses_inventory_with_apply_before_touching_the_bridge():
    recipe = [{"action": "eq.high_pass", "target": "vocal", "schema": SCHEMA,
               "freq_hz": 25}]
    paths = _write_temp({"recipe.json": recipe, "inventory.json": {}})
    try:
        code, output = _run_main([
            "--recipe", paths["recipe.json"], "--project", PROJECT,
            "--inventory", paths["inventory.json"], "--apply"])
    finally:
        _cleanup(paths)
    assert code == 1
    assert "--inventory is dry-run only" in output


def test_cli_reports_recipe_violations_as_data():
    recipe = [{"action": "pan", "target": "vocal", "position": 0.0}]
    paths = _write_temp({"recipe.json": recipe})
    try:
        code, output = _run_main([
            "--recipe", paths["recipe.json"], "--project", PROJECT,
            "--tracks", "vocal=3:Lead Vocal"])
    finally:
        _cleanup(paths)
    assert code == 2
    report = json.loads(output)
    assert any(item["code"] == "schema_mismatch"
               for item in report["violations"])


def _write_temp(files):
    directory = tempfile.mkdtemp()
    paths = {}
    for name, payload in files.items():
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        paths[name] = path
    return paths


def _cleanup(paths):
    for path in paths.values():
        try:
            os.unlink(path)
        except OSError:
            pass
    directory = os.path.dirname(next(iter(paths.values())))
    try:
        os.rmdir(directory)
    except OSError:
        pass


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ok  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} fallo(s)")
    sys.exit(1 if failures else 0)
