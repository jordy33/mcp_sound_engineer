"""Normalize ``logic_plugins.get_inventory`` into a write-safe slot chain.

The real response (mcp_logic HEAD ``b42cdd37``) is an Honest Contract v2
envelope.  State A carries ``complete`` and ``plugins[]``; every item carries
``insert``/``read_status``/``occupied``/``name``/``plugin_id``/``bypassed`` and,
for a complete track read, an optional ``plugin_insert_ref``.  The master
Stereo Output strip (``strip: "output"``) never receives refs.

Two rules matter for safety and are enforced here rather than by convention:

* State B/C, ``complete: false``, unreadable/unclassified slots, unknown read
  statuses, duplicate insert indices and selector echoes that do not match the
  request all make the chain *unreliable*.  An unreliable chain never
  authorises a write.
* Only ``read_status == "empty"`` with no values is a writable slot.  A slot
  that merely looks unoccupied is not enough.

Nothing in this module mutates Logic; it only reads and classifies.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .mcp_logic_contract import MASTER_SELECTOR, MASTER_STRIP

INVENTORY_SCHEMA_VERSION = 2
READ_STATUSES = frozenset({"empty", "ok", "unreadable", "unclassified"})
MASTER_KEY = "master"


class InventoryError(ValueError):
    """The chain is not reliable enough to authorise the requested action."""

    def __init__(self, message: str, issues=()):
        self.issues = tuple(issues)
        super().__init__(message)


@dataclass(frozen=True)
class Slot:
    insert: int
    read_status: str
    occupied: bool | None
    name: str | None = None
    plugin_id: str | None = None
    bypassed: bool | None = None
    plugin_insert_ref: str | None = None

    @property
    def readable(self) -> bool:
        return (self.read_status == "ok" and self.occupied is True
                and isinstance(self.name, str) and bool(self.name.strip()))

    @property
    def empty(self) -> bool:
        """The only write-safe slot state; every value field must be absent."""
        return (self.read_status == "empty" and self.occupied is False
                and self.name is None and self.plugin_id is None
                and self.bypassed is None)

    @property
    def has_ref(self) -> bool:
        return isinstance(self.plugin_insert_ref, str) and bool(self.plugin_insert_ref)

    @property
    def unreadable(self) -> bool:
        return self.read_status in ("unreadable", "unclassified")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class InventoryChain:
    key: str
    selector: dict
    state: str | None = None
    complete: bool = False
    slots: tuple = ()
    track: int | None = None
    strip: str | None = None
    track_name: str | None = None
    mixer_strip_index: int | None = None
    reason: str | None = None
    error: str | None = None
    issues: tuple = ()

    @property
    def is_master(self) -> bool:
        return self.strip == MASTER_STRIP

    @property
    def state_a(self) -> bool:
        return self.state == "A"

    @property
    def reliable(self) -> bool:
        """State A + complete + no structural drift.  The write precondition."""
        return self.state_a and self.complete and not self.issues

    @property
    def readable_slots(self) -> tuple:
        return tuple(slot for slot in self.slots if slot.readable)

    @property
    def occupied_slots(self) -> tuple:
        return tuple(slot for slot in self.slots if slot.occupied is True)

    def slot(self, insert: int) -> Slot | None:
        for slot in self.slots:
            if slot.insert == insert:
                return slot
        return None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "selector": dict(self.selector),
            "state": self.state,
            "complete": self.complete,
            "reliable": self.reliable,
            "track": self.track,
            "strip": self.strip,
            "track_name": self.track_name,
            "mixer_strip_index": self.mixer_strip_index,
            "reason": self.reason,
            "error": self.error,
            "issues": list(self.issues),
            "plugins": [slot.to_dict() for slot in self.slots],
        }


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_string(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _selector_from_payload(payload: dict, issues: list) -> dict:
    has_track = _is_int(payload.get("track")) and payload["track"] >= 0
    has_strip = payload.get("strip") == MASTER_STRIP
    raw_track = payload.get("track")
    raw_strip = payload.get("strip")
    if has_track and has_strip:
        issues.append("ambiguous_selector")
        return {}
    if has_strip:
        return dict(MASTER_SELECTOR)
    if has_track:
        return {"track": raw_track}
    if raw_track is not None:
        issues.append("invalid_track_echo")
    if raw_strip is not None:
        issues.append("invalid_strip_echo")
    else:
        issues.append("missing_selector")
    return {}


def _validate_selector(selector, payload: dict, issues: list) -> tuple:
    """Return (selector, is_master) and append selector-echo issues."""
    if selector is None:
        derived = _selector_from_payload(payload, issues)
        return derived, derived.get("strip") == MASTER_STRIP

    if not isinstance(selector, dict):
        issues.append("selector_not_object")
        return {}, False
    if ("track" in selector) == ("strip" in selector):
        issues.append("invalid_selector")
        return dict(selector), False

    if "strip" in selector:
        if selector["strip"] != MASTER_STRIP:
            issues.append("unsupported_strip")
        elif payload.get("strip") != MASTER_STRIP:
            issues.append("selector_mismatch")
        if payload.get("track") is not None:
            issues.append("selector_mismatch")
        return {"strip": selector["strip"]}, True

    track = selector["track"]
    if not _is_int(track) or track < 0:
        issues.append("invalid_selector")
        return {"track": track}, False
    if payload.get("track") != track:
        issues.append("selector_mismatch")
    if payload.get("strip") is not None:
        issues.append("selector_mismatch")
    return {"track": track}, False


def _parse_slot(item, position: int, issues: list) -> Slot | None:
    if not isinstance(item, dict):
        issues.append("slot_not_object:%s" % position)
        return None
    insert = item.get("insert")
    if not _is_int(insert) or insert < 0:
        issues.append("invalid_insert:%s" % position)
        return None

    for field in ("insert", "read_status", "occupied", "name", "plugin_id",
                  "bypassed"):
        if field not in item:
            issues.append("missing_field:%s:%s" % (field, insert))

    read_status = item.get("read_status")
    if not isinstance(read_status, str):
        issues.append("missing_read_status:%s" % insert)
        read_status = ""
    elif read_status not in READ_STATUSES:
        issues.append("unknown_read_status:%s:%s" % (read_status, insert))

    occupied = item.get("occupied")
    if not isinstance(occupied, bool):
        issues.append("occupied_not_bool:%s" % insert)
        occupied = None

    name = item.get("name")
    if name is not None and not _clean_string(name):
        issues.append("malformed_name:%s" % insert)
        name = None

    plugin_id = item.get("plugin_id")
    if plugin_id is not None and not _clean_string(plugin_id):
        issues.append("malformed_plugin_id:%s" % insert)
        plugin_id = None

    bypassed = item.get("bypassed")
    if bypassed is not None and not isinstance(bypassed, bool):
        issues.append("malformed_bypassed:%s" % insert)
        bypassed = None

    ref = item.get("plugin_insert_ref")
    if ref is not None and not _clean_string(ref):
        issues.append("malformed_ref:%s" % insert)
        ref = None

    if read_status == "empty":
        if not (occupied is False and name is None and plugin_id is None
                and bypassed is None):
            issues.append("empty_with_values:%s" % insert)
    elif read_status == "ok":
        if occupied is not True or name is None:
            issues.append("ok_without_name:%s" % insert)

    return Slot(insert=insert, read_status=read_status, occupied=occupied,
                name=name, plugin_id=plugin_id, bypassed=bypassed,
                plugin_insert_ref=ref)


def parse_inventory(payload, key: str = "", selector=None) -> InventoryChain:
    """Classify one ``get_inventory`` payload (structuredContent dict)."""
    issues: list = []
    if not isinstance(payload, dict):
        return InventoryChain(key=key, selector={}, issues=("payload_not_object",))

    state = payload.get("state") if isinstance(payload.get("state"), str) else None
    if state is None:
        issues.append("missing_state")
    if payload.get("success") is not True:
        issues.append("not_success")
    if payload.get("verified") is not True:
        issues.append("not_verified")
    hc_schema = payload.get("hc_schema")
    if not _is_int(hc_schema):
        issues.append("missing_hc_schema")
    elif hc_schema != INVENTORY_SCHEMA_VERSION:
        issues.append("wrong_hc_schema:%s" % hc_schema)

    chain_selector, is_master = _validate_selector(selector, payload, issues)
    if not key:
        if is_master:
            key = MASTER_KEY
        elif _is_int(payload.get("track")):
            key = "track:%s" % payload["track"]

    track = payload.get("track") if _is_int(payload.get("track")) else None
    strip = payload.get("strip") if isinstance(payload.get("strip"), str) else None
    track_name = _clean_string(payload.get("track_name"))
    mixer_index = payload.get("mixer_strip_index")
    mixer_index = mixer_index if _is_int(mixer_index) else None
    reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
    error = payload.get("error") if isinstance(payload.get("error"), str) else None
    complete = payload.get("complete")
    if not isinstance(complete, bool):
        issues.append("complete_missing")
        complete = False
    elif not complete:
        issues.append("incomplete_inventory")

    slots: list = []
    plugins = payload.get("plugins")
    if state == "A":
        if not isinstance(plugins, list):
            issues.append("plugins_missing")
        else:
            seen = set()
            for position, item in enumerate(plugins):
                slot = _parse_slot(item, position, issues)
                if slot is None:
                    continue
                if slot.insert in seen:
                    issues.append("duplicate_insert:%s" % slot.insert)
                seen.add(slot.insert)
                slots.append(slot)
            slots.sort(key=lambda slot: slot.insert)
            if not slots:
                issues.append("no_slots")
            if complete and any(slot.unreadable for slot in slots):
                issues.append("incomplete_claimed")
            refs = [slot for slot in slots if slot.has_ref]
            if refs and is_master:
                issues.append("master_ref_unexpected")
            elif refs and not complete:
                issues.append("ref_on_incomplete_inventory")
    else:
        if state == "B":
            issues.append("state_b:%s" % (reason or "unavailable"))
        elif state == "C":
            issues.append("state_c:%s" % (error or "error"))
        elif state is not None:
            issues.append("unknown_state:%s" % state)

    return InventoryChain(
        key=key or "target",
        selector=chain_selector,
        state=state,
        complete=complete,
        slots=tuple(slots),
        track=track,
        strip=strip,
        track_name=track_name,
        mixer_strip_index=mixer_index,
        reason=reason,
        error=error,
        issues=tuple(issues),
    )


def from_tool_result(result, key: str = "", selector=None) -> InventoryChain:
    """Normalize a ``mcp_client.ToolResult`` without importing that module."""
    if not getattr(result, "ok", False):
        return InventoryChain(
            key=key or "target",
            selector=dict(selector or {}),
            error=getattr(result, "error", None),
            issues=("transport:%s" % (getattr(result, "error", None) or "unavailable"),),
        )
    return parse_inventory(getattr(result, "payload", None), key=key,
                           selector=selector)


def fetch_chain(client, selector: dict, key: str = "") -> InventoryChain:
    """Read one live inventory chain.  Never mutates; errors become issues."""
    try:
        result = client.get_inventory(selector)
    except Exception as exc:  # noqa: BLE001 - transport errors become data
        return InventoryChain(
            key=key or "target", selector=dict(selector),
            issues=("transport:%s" % exc,))
    return from_tool_result(result, key=key, selector=selector)


# ---------------------------------------------------------------------------
# deterministic queries over a normalized chain
# ---------------------------------------------------------------------------
def find_plugins(chain: InventoryChain, plugin_id: str | None = None,
                 name: str | None = None) -> tuple:
    """Readable occupied slots matching a canonical id and/or display name."""
    matches = []
    for slot in chain.slots:
        if not slot.readable:
            continue
        if plugin_id is not None and slot.plugin_id != plugin_id:
            continue
        if name is not None and slot.name != name:
            continue
        matches.append(slot)
    return tuple(matches)


def duplicate_plugin(chain: InventoryChain, plugin_id: str) -> bool:
    return len(find_plugins(chain, plugin_id=plugin_id)) > 1


def first_empty_slot(chain: InventoryChain) -> Slot | None:
    """First verified-empty slot, or None.  Check ``chain.reliable`` first."""
    for slot in chain.slots:
        if slot.empty:
            return slot
    return None


def require_readable(chain: InventoryChain) -> InventoryChain:
    """Raise unless the chain is State A, complete and drift-free."""
    if chain.reliable:
        return chain
    detail = ", ".join(chain.issues) or chain.state or "unavailable"
    raise InventoryError("inventory for %s is not reliable: %s"
                         % (chain.key, detail), chain.issues)


def verify_insert(chain: InventoryChain, insert: int,
                  plugin_id: str | None = None) -> tuple:
    """Confirm a freshly inserted plugin.  Return ``(slot, issue)``.

    ``issue`` is None only when the chain is reliable, the slot is readable,
    the identity matches (when supplied) and the plugin is not bypassed or
    bypass-unknown.  Callers must not write parameters on a non-None issue.
    """
    if not chain.reliable:
        return None, "inventory_not_reliable"
    slot = chain.slot(insert)
    if slot is None:
        return None, "slot_missing"
    if not slot.readable:
        return None, "slot_not_readable"
    if plugin_id is not None and slot.plugin_id != plugin_id:
        return None, "plugin_mismatch"
    if slot.bypassed is not False:
        return None, "plugin_bypassed_or_unknown"
    return slot, None


# ---------------------------------------------------------------------------
# offline inventory files (dry-run only; apply always re-reads live)
# ---------------------------------------------------------------------------
def is_inventory_response(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("state"), str)


@dataclass(frozen=True)
class InventoryFile:
    responses: dict
    single: bool

    @classmethod
    def load(cls, path: str) -> "InventoryFile":
        with open(path) as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("inventory file must contain a JSON object")
        if is_inventory_response(data):
            return cls(responses={"*": data}, single=True)
        for key, value in data.items():
            if not isinstance(value, dict):
                raise ValueError("inventory entry %r must be a JSON object" % key)
        return cls(responses=dict(data), single=False)

    def response_for(self, keys, single_target: bool = False):
        for key in keys:
            if key in self.responses:
                return self.responses[key]
        if self.single and single_target:
            return self.responses.get("*")
        return None

    def chain_for(self, keys, single_target: bool = False, selector=None):
        key = next(iter(keys), "") if keys else ""
        raw = self.response_for(keys, single_target=single_target)
        if raw is None:
            return None
        return parse_inventory(raw, key=key, selector=selector)
