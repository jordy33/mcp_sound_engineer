"""Normalization tests for adapter.inventory.

The payloads mirror the real State A/B/C shapes measured in mcp_logic HEAD
b42cdd37: every item carries insert/read_status/occupied/name/plugin_id/
bypassed, refs appear only on complete track reads, and the master strip is
addressed with strip:"output".
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adapter.inventory import (  # noqa: E402
    InventoryError,
    InventoryFile,
    Slot,
    duplicate_plugin,
    fetch_chain,
    find_plugins,
    first_empty_slot,
    from_tool_result,
    parse_inventory,
    require_readable,
    verify_insert,
)
from adapter.mcp_client import ToolResult  # noqa: E402
from adapter.mcp_logic_contract import (  # noqa: E402
    CHANNEL_EQ_PLUGIN_ID,
    COMPRESSOR_PLUGIN_ID,
    MASTER_SELECTOR,
)

EQ_REF = "ins_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
COMP_REF = "ins_cccccccccccccccccccccccccccccccc"


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
    return _slot(insert, "ok", name="Channel EQ", plugin_id=CHANNEL_EQ_PLUGIN_ID,
                 bypassed=False, ref=ref)


def _compressor_slot(insert=2, ref=COMP_REF):
    return _slot(insert, "ok", name="Compressor", plugin_id=COMPRESSOR_PLUGIN_ID,
                 bypassed=False, ref=ref)


def _track_payload(track=3, *, plugins=None, complete=True, state="A",
                   success=True, verified=True, hc_schema=2, refs=True):
    if plugins is None:
        plugins = [_eq_slot(0, EQ_REF if refs else None),
                   _slot(1, "empty"),
                   _compressor_slot(2, COMP_REF if refs else None)]
    payload = {
        "success": success, "verified": verified, "state": state,
        "hc_schema": hc_schema, "operation": "logic_plugins.get_inventory",
        "track": track, "strip": None, "complete": complete,
        "track_name": "Lead Vocal", "mixer_strip_index": track + 1,
        "plugins": plugins,
    }
    return payload


def _master_payload(*, plugins=None, complete=True, state="A", track_name="Stereo Out"):
    if plugins is None:
        plugins = [_eq_slot(0, None), _slot(1, "empty")]
    return {
        "success": True, "verified": state == "A", "state": state,
        "hc_schema": 2, "operation": "logic_plugins.get_inventory",
        "track": None, "strip": "output", "complete": complete,
        "track_name": track_name, "mixer_strip_index": 0,
        "plugins": plugins if state == "A" else None,
    }


def _track_chain(**overrides):
    selector = overrides.pop("selector", {"track": 3})
    return parse_inventory(_track_payload(**overrides), key="vocal",
                           selector=selector)


def _issues(chain):
    return list(chain.issues)


def _raises(exc_type, fn, needle=None):
    try:
        fn()
    except exc_type as exc:
        if needle is not None:
            assert needle in str(exc), (needle, str(exc))
        return exc
    raise AssertionError("expected %s" % exc_type.__name__)


# ---------------------------------------------------------------------------
# State A normalization
# ---------------------------------------------------------------------------
def test_track_inventory_normalizes_slots_and_refs():
    chain = _track_chain()
    assert chain.state == "A"
    assert chain.state_a is True
    assert chain.complete is True
    assert chain.reliable is True
    assert chain.issues == ()
    assert chain.key == "vocal"
    assert chain.selector == {"track": 3}
    assert chain.track == 3 and chain.strip is None
    assert chain.track_name == "Lead Vocal"
    assert chain.mixer_strip_index == 4
    assert [slot.insert for slot in chain.slots] == [0, 1, 2]

    eq, empty, comp = chain.slots
    assert isinstance(eq, Slot)
    assert eq.readable is True and eq.empty is False
    assert eq.name == "Channel EQ"
    assert eq.plugin_id == CHANNEL_EQ_PLUGIN_ID
    assert eq.bypassed is False
    assert eq.plugin_insert_ref == EQ_REF and eq.has_ref is True
    assert empty.empty is True and empty.readable is False
    assert empty.name is None and empty.plugin_id is None and empty.bypassed is None
    assert comp.plugin_id == COMPRESSOR_PLUGIN_ID


def test_master_inventory_is_strip_only_and_carries_no_refs():
    chain = parse_inventory(_master_payload(), key="master",
                            selector=MASTER_SELECTOR)
    assert chain.reliable is True
    assert chain.is_master is True
    assert chain.selector == {"strip": "output"}
    assert chain.track is None and chain.strip == "output"
    assert chain.track_name == "Stereo Out"
    assert all(slot.plugin_insert_ref is None for slot in chain.slots)

    derived = parse_inventory(_master_payload())
    assert derived.key == "master"
    assert derived.selector == MASTER_SELECTOR
    assert derived.reliable is True


def test_master_ref_is_a_structural_drift():
    payload = _master_payload(plugins=[_eq_slot(0, EQ_REF), _slot(1, "empty")])
    chain = parse_inventory(payload, key="master", selector=MASTER_SELECTOR)
    assert chain.reliable is False
    assert "master_ref_unexpected" in _issues(chain)


# ---------------------------------------------------------------------------
# Non-State-A / incomplete chains
# ---------------------------------------------------------------------------
def test_state_b_is_not_reliable():
    payload = {
        "success": True, "verified": False, "state": "B", "hc_schema": 2,
        "operation": "logic_plugins.get_inventory", "reason": "readback_unavailable",
        "track": 3, "strip": None,
    }
    chain = parse_inventory(payload, key="vocal", selector={"track": 3})
    assert chain.state == "B"
    assert chain.state_a is False
    assert chain.reliable is False
    assert chain.reason == "readback_unavailable"
    assert chain.slots == ()
    assert "state_b:readback_unavailable" in _issues(chain)


def test_state_c_is_not_reliable_and_keeps_error():
    payload = {
        "success": False, "verified": False, "state": "C", "hc_schema": 2,
        "operation": "logic_plugins.get_inventory", "error": "invalid_params",
        "track": None, "strip": "output",
    }
    chain = parse_inventory(payload, key="master", selector=MASTER_SELECTOR)
    assert chain.reliable is False
    assert chain.error == "invalid_params"
    assert "state_c:invalid_params" in _issues(chain)


def test_missing_or_wrong_hc_schema_is_not_reliable():
    for hc_schema, needle in ((None, "missing_hc_schema"), (1, "wrong_hc_schema:1")):
        payload = _track_payload(hc_schema=hc_schema)
        if hc_schema is None:
            payload.pop("hc_schema")
        chain = parse_inventory(payload, selector={"track": 3})
        assert chain.reliable is False, hc_schema
        assert needle in _issues(chain), (needle, _issues(chain))


def test_missing_success_or_verified_is_not_reliable():
    for field in ("success", "verified"):
        payload = _track_payload()
        payload.pop(field)
        chain = parse_inventory(payload, selector={"track": 3})
        assert chain.reliable is False, field
        assert "not_%s" % field in _issues(chain)


def test_complete_false_blocks_reuse_but_keeps_slots():
    payload = _track_payload(
        complete=False,
        plugins=[_eq_slot(), _slot(1, "unreadable", occupied=True),
                 _slot(2, "empty")])
    chain = parse_inventory(payload, selector={"track": 3})
    assert chain.complete is False
    assert chain.reliable is False
    assert "incomplete_inventory" in _issues(chain)
    assert len(chain.slots) == 3
    assert chain.slots[1].unreadable is True
    assert chain.slots[1].readable is False
    assert chain.slots[2].empty is True


def test_complete_true_with_unreadable_slot_is_inconsistent():
    payload = _track_payload(
        complete=True,
        plugins=[_eq_slot(), _slot(1, "unreadable", occupied=True)])
    chain = parse_inventory(payload, selector={"track": 3})
    assert chain.reliable is False
    assert "incomplete_claimed" in _issues(chain)


def test_unreadable_ref_on_incomplete_inventory_is_flagged():
    payload = _track_payload(
        complete=False,
        plugins=[_eq_slot(), _slot(1, "unreadable", occupied=True)])
    chain = parse_inventory(payload, selector={"track": 3})
    assert "ref_on_incomplete_inventory" in _issues(chain)
    assert chain.reliable is False


def test_unknown_read_status_is_not_addressable():
    payload = _track_payload(
        complete=False,
        plugins=[_slot(0, "weird", occupied=True, name="Mystery")])
    chain = parse_inventory(payload, selector={"track": 3})
    assert chain.reliable is False
    assert "unknown_read_status:weird:0" in _issues(chain)
    slot = chain.slots[0]
    assert slot.readable is False and slot.empty is False


def test_duplicate_insert_is_rejected():
    payload = _track_payload(
        complete=False,
        plugins=[_eq_slot(1), _compressor_slot(1)])
    chain = parse_inventory(payload, selector={"track": 3})
    assert "duplicate_insert:1" in _issues(chain)
    assert chain.reliable is False


def test_empty_slot_with_values_is_not_writable():
    payload = _track_payload(
        plugins=[_slot(0, "empty", occupied=False, name="Ghost")])
    chain = parse_inventory(payload, selector={"track": 3})
    assert "empty_with_values:0" in _issues(chain)
    assert chain.slots[0].empty is False
    assert chain.reliable is False


def test_malformed_fields_are_reported_and_nulled():
    item = _slot(0, "ok", name="Channel EQ", plugin_id=CHANNEL_EQ_PLUGIN_ID,
                 bypassed=False)
    item.pop("bypassed")
    item["name"] = 42
    payload = _track_payload(plugins=[item])
    chain = parse_inventory(payload, selector={"track": 3})
    assert "missing_field:bypassed:0" in _issues(chain)
    assert "malformed_name:0" in _issues(chain)
    assert chain.slots[0].name is None
    assert chain.slots[0].readable is False


def test_no_slots_state_a_is_inconsistent():
    chain = parse_inventory(_track_payload(plugins=[]), selector={"track": 3})
    assert "no_slots" in _issues(chain)
    assert chain.reliable is False


# ---------------------------------------------------------------------------
# selector echo validation
# ---------------------------------------------------------------------------
def test_selector_mismatch_is_rejected():
    chain = parse_inventory(_track_payload(track=3), selector={"track": 4})
    assert "selector_mismatch" in _issues(chain)
    assert chain.reliable is False

    wrong_kind = parse_inventory(_track_payload(track=3),
                                 selector=MASTER_SELECTOR)
    assert "selector_mismatch" in _issues(wrong_kind)
    assert wrong_kind.reliable is False


def test_ambiguous_selector_echo_is_rejected():
    payload = _track_payload()
    payload["strip"] = "output"
    chain = parse_inventory(payload)
    assert "ambiguous_selector" in _issues(chain)
    assert chain.reliable is False


def test_payload_that_is_not_an_object_is_rejected():
    chain = parse_inventory("nope", key="vocal")
    assert chain.issues == ("payload_not_object",)
    assert chain.reliable is False


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------
def test_find_plugins_and_duplicates():
    chain = _track_chain()
    assert find_plugins(chain, plugin_id=CHANNEL_EQ_PLUGIN_ID) == (chain.slots[0],)
    assert find_plugins(chain, name="Compressor") == (chain.slots[2],)
    assert find_plugins(chain, plugin_id="logic.stock.effect.limiter") == ()
    assert duplicate_plugin(chain, CHANNEL_EQ_PLUGIN_ID) is False

    payload = _track_payload(plugins=[_eq_slot(0), _eq_slot(2)])
    dup = parse_inventory(payload, selector={"track": 3})
    assert duplicate_plugin(dup, CHANNEL_EQ_PLUGIN_ID) is True
    assert len(find_plugins(dup, plugin_id=CHANNEL_EQ_PLUGIN_ID)) == 2


def test_first_empty_slot_skips_malformed_empties():
    payload = _track_payload(
        plugins=[_slot(0, "empty", occupied=False, name="Ghost"),
                 _slot(1, "empty")])
    chain = parse_inventory(payload, selector={"track": 3})
    empty = first_empty_slot(chain)
    assert empty is not None and empty.insert == 1
    assert first_empty_slot(_track_chain(plugins=[_eq_slot()])) is None


def test_require_readable_raises_with_issues():
    assert require_readable(_track_chain()).key == "vocal"
    payload = {
        "success": True, "verified": False, "state": "B", "hc_schema": 2,
        "reason": "readback_unavailable", "track": 3, "strip": None,
    }
    chain = parse_inventory(payload, key="vocal", selector={"track": 3})
    exc = _raises(InventoryError, lambda: require_readable(chain),
                  "state_b:readback_unavailable")
    assert "state_b:readback_unavailable" in exc.issues


def test_verify_insert_requires_a_reliable_readable_unbypassed_slot():
    chain = _track_chain()
    slot, issue = verify_insert(chain, 0, CHANNEL_EQ_PLUGIN_ID)
    assert slot is chain.slots[0] and issue is None

    assert verify_insert(chain, 0, COMPRESSOR_PLUGIN_ID)[1] == "plugin_mismatch"
    assert verify_insert(chain, 1, CHANNEL_EQ_PLUGIN_ID)[1] == "slot_not_readable"
    assert verify_insert(chain, 9, CHANNEL_EQ_PLUGIN_ID)[1] == "slot_missing"

    payload = _track_payload(plugins=[
        _slot(0, "ok", name="Channel EQ", plugin_id=CHANNEL_EQ_PLUGIN_ID,
              bypassed=True)])
    bypassed = parse_inventory(payload, selector={"track": 3})
    assert verify_insert(bypassed, 0, CHANNEL_EQ_PLUGIN_ID)[1] == \
        "plugin_bypassed_or_unknown"

    unknown = parse_inventory(
        _track_payload(plugins=[_slot(0, "ok", name="Channel EQ",
                                      plugin_id=CHANNEL_EQ_PLUGIN_ID)]),
        selector={"track": 3})
    assert unknown.slots[0].bypassed is None
    assert verify_insert(unknown, 0, CHANNEL_EQ_PLUGIN_ID)[1] == \
        "plugin_bypassed_or_unknown"

    payload = _track_payload(
        complete=False,
        plugins=[_eq_slot(), _slot(1, "unreadable", occupied=True)])
    unreliable = parse_inventory(payload, selector={"track": 3})
    assert verify_insert(unreliable, 0, CHANNEL_EQ_PLUGIN_ID)[1] == \
        "inventory_not_reliable"


# ---------------------------------------------------------------------------
# tool result / live read wrapper
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.selectors = []

    def get_inventory(self, selector):
        self.selectors.append(dict(selector))
        if self.error is not None:
            raise self.error
        return self.result


def test_fetch_chain_parses_a_tool_result():
    client = FakeClient(ToolResult(ok=True, payload=_track_payload()))
    chain = fetch_chain(client, {"track": 3}, key="vocal")
    assert chain.reliable is True
    assert client.selectors == [{"track": 3}]


def test_fetch_chain_turns_transport_failures_into_issues():
    failed = FakeClient(ToolResult(ok=False, error="cannot reach bridge"))
    chain = fetch_chain(failed, {"track": 3}, key="vocal")
    assert chain.reliable is False
    assert "transport:cannot reach bridge" in chain.issues

    raising = FakeClient(error=RuntimeError("connection refused"))
    chain = fetch_chain(raising, MASTER_SELECTOR, key="master")
    assert chain.reliable is False
    assert any(issue.startswith("transport:") for issue in chain.issues)


def test_from_tool_result_keeps_the_selector():
    chain = from_tool_result(ToolResult(ok=True, payload=_master_payload()),
                             key="master", selector=MASTER_SELECTOR)
    assert chain.selector == MASTER_SELECTOR
    assert chain.reliable is True


def test_chain_to_dict_is_report_ready():
    data = _track_chain().to_dict()
    assert data["key"] == "vocal"
    assert data["reliable"] is True
    assert data["plugins"][0]["plugin_id"] == CHANNEL_EQ_PLUGIN_ID
    assert data["plugins"][0]["plugin_insert_ref"] == EQ_REF
    json.dumps(data)


# ---------------------------------------------------------------------------
# offline inventory files
# ---------------------------------------------------------------------------
def _write_temp(payload):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    try:
        json.dump(payload, handle)
    finally:
        handle.close()
    return handle.name


def test_inventory_file_keyed_by_target():
    path = _write_temp({"vocal": _track_payload(),
                        "master": _master_payload()})
    try:
        source = InventoryFile.load(path)
    finally:
        os.unlink(path)
    assert source.single is False
    chain = source.chain_for(["vocal", "01_vocals.wav"], selector={"track": 3})
    assert chain is not None and chain.reliable is True
    assert source.response_for(["01_vocals.wav"], single_target=True) is None
    assert source.chain_for(["missing"], selector={"track": 3}) is None
    master = source.chain_for(["master"], selector=MASTER_SELECTOR)
    assert master.reliable is True and master.is_master is True


def test_inventory_file_with_a_single_response():
    path = _write_temp(_track_payload())
    try:
        source = InventoryFile.load(path)
    finally:
        os.unlink(path)
    assert source.single is True
    assert source.chain_for(["vocal"], single_target=True,
                            selector={"track": 3}).reliable is True
    assert source.response_for(["vocal"], single_target=False) is None


def test_inventory_file_rejects_non_object_entries():
    path = _write_temp({"vocal": ["not", "an", "object"]})
    try:
        _raises(ValueError, lambda: InventoryFile.load(path),
                "must be a JSON object")
    finally:
        os.unlink(path)


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
