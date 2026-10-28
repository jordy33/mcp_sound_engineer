"""Closed-loop tests with a fake bridge and fake measurements.

The fake bridge mirrors mcp_logic's verified export/plugin surface closely
enough to exercise the loop's own logic: inventory-driven inserts, State A
gating, artifact fingerprints, residual windows, correction arithmetic and
every stop reason that does not need Logic.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adapter import mcp_logic_contract as contract  # noqa: E402
from adapter.loop import LoopConfig, run_loop, sha256_file  # noqa: E402
from adapter.mcp_client import ToolResult  # noqa: E402
from server import rules  # noqa: E402

SCHEMA = rules.RECIPE_SCHEMA_VERSION
TRACK = 3
EQ_REF = "ins_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PROJECT = "/tmp/session.logicx"
BANDS = contract.EQ_VERIFIED_BANDS


# ---------------------------------------------------------------------------
# recipes and metrics
# ---------------------------------------------------------------------------
def _action(name, target="vocal", **fields):
    action = {"action": name, "target": target, "schema": SCHEMA}
    action.update(fields)
    return action


def _bell(target="vocal", freq=800, gain=-2.0, q=2.0, **fields):
    return _action("eq.bell", target, freq_hz=freq, gain_db=gain, q=q, **fields)


def _shelf(target="vocal", band="Low Shelf", freq=120, gain=-2.5, **fields):
    return _action("eq.shelf", target, band_name=band, freq_hz=freq,
                   gain_db=gain, **fields)


def _limiter(target="master", ceiling=-1.0):
    return _action("limiter", target, ceiling_dbtp=ceiling,
                   input_gain_db=0.0, expected_gain_reduction_db=1.0)


def _compressor(target="vocal"):
    return _action("compressor", target, gain_reduction_db=2.0)


def _send_back():
    return _action("send_back", "master", reasons=["low_confidence"])


def _metrics(**overrides):
    base = {
        "sample_rate": 44100, "channels": 2, "duration_seconds": 10.0,
        "silence_ratio": 0.0,
        "loudness": {"integrated_lufs": -14.0, "true_peak_dbtp": -1.0,
                     "sample_peak_dbfs": -1.2, "crest_db": 12.0,
                     "loudness_range_lu": 6.0},
        "clipping": {"count": 0},
        "stereo": {"phase_correlation": 0.85, "mono_cancel_db": -0.5},
        "spectral": {"sibilance_db_rel": -2.0, "bands_db_rel": {},
                     "centroid_hz": 1000.0},
        "noise": {"floor_dbfs": -80.0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = dict(base[key], **value)
        else:
            base[key] = value
    return base


def _measure_sequence(items):
    queue = [copy.deepcopy(item) for item in items]

    def measure(path):
        return queue.pop(0) if queue else _metrics()

    return measure


def _bands(value, *centers):
    return [{"centerHz": center, "residualDb": value} for center in centers]


# ---------------------------------------------------------------------------
# fake bridge
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


def _empty_payload(track):
    return {
        "success": True, "verified": True, "state": "A", "hc_schema": 2,
        "operation": "logic_plugins.get_inventory",
        "track": track, "strip": None, "complete": True,
        "track_name": "Lead Vocal", "mixer_strip_index": track + 1,
        "plugins": [_slot(0, "empty"), _slot(1, "empty"),
                    _slot(2, "empty"), _slot(3, "empty")],
    }


def _empty_master_payload():
    return {
        "success": True, "verified": True, "state": "A", "hc_schema": 2,
        "operation": "logic_plugins.get_inventory",
        "track": None, "strip": "output", "complete": True,
        "track_name": "Stereo Out", "mixer_strip_index": 0,
        "plugins": [_slot(0, "empty"), _slot(1, "empty"),
                    _slot(2, "empty"), _slot(3, "empty")],
    }


def _existing_track_payload(track=TRACK, ref=EQ_REF):
    payload = _empty_payload(track)
    payload["plugins"][0] = _slot(
        0, "ok", name="Channel EQ",
        plugin_id=contract.CHANNEL_EQ_PLUGIN_ID, bypassed=False, ref=ref)
    return payload


def _field(value):
    return {"read_status": "read", "observed_raw": value,
            "raw_unit": None, "observed_display": str(value),
            "display_read_status": "read"}


class FakeBridge:
    def __init__(self, *, settings=None, settings_sequence=None,
                 comparisons=None, comparison_complete=True,
                 comparison_override=None, fingerprint_override=None,
                 plan_override=None, run_override=None, insert_state="A",
                 write_state="A", artifact_extension=".wav",
                 existing=None, enabled=None, state_payload_override=None,
                 fail_inventory=False, run_path_outside=None,
                 plan_path_outside=False, settings_status="ok"):
        self.settings = settings if settings is not None else {
            "format": "wav", "bit_depth": 24, "sample_rate": 44100}
        self.settings_status = settings_status
        self.settings_sequence = list(settings_sequence or [])
        self.comparisons = list(comparisons or [])
        self.comparison_complete = comparison_complete
        self.comparison_override = comparison_override
        self.fingerprint_override = fingerprint_override
        self.plan_override = plan_override
        self.run_override = run_override
        self.insert_state = insert_state
        self.write_state = write_state
        self.artifact_extension = artifact_extension
        self.existing = dict(existing or {})
        self.enabled = dict(enabled or {})
        self.state_payload_override = state_payload_override
        self.fail_inventory = fail_inventory
        self.run_path_outside = run_path_outside
        self.plan_path_outside = plan_path_outside

        self.inserted = {}
        self.refs = {}
        self.gains = {}
        self.calls = []
        self.inventory_reads = []
        self.exports = []
        self.comparison_pairs = []
        self.artifacts = []
        self.run_counter = 0
        self.state_reads = 0
        self._register_existing_refs()

    # -- helpers -----------------------------------------------------------
    def _register_existing_refs(self):
        for key, payload in self.existing.items():
            for item in payload.get("plugins") or []:
                ref = item.get("plugin_insert_ref")
                if ref:
                    self.refs[ref] = key

    def _key_for(self, selector):
        if isinstance(selector, dict) and selector.get("strip") == "output":
            return "master"
        return "track:%d" % selector["track"]

    def _key_for_params(self, params):
        if params.get("strip") == contract.MASTER_STRIP:
            return "master"
        if "track" in params:
            return "track:%d" % params["track"]
        ref = params.get("target_ref")
        if ref in self.refs:
            return self.refs[ref]
        raise AssertionError("cannot resolve target_ref %r" % (ref,))

    def _pop_state(self, value):
        if isinstance(value, list):
            return (value.pop(0) if value else "A")
        return value

    def _state_result(self, tool, command, state, payload=None):
        if state == "A":
            body = {"success": True, "verified": True, "state": "A",
                    "hc_schema": 2}
            if payload:
                body.update(payload)
            return ToolResult(ok=True, success=True, verified=True, state="A",
                              hc_schema=2, payload=body, tool=tool,
                              command=command)
        if state == "B":
            body = {"success": True, "verified": False, "state": "B",
                    "hc_schema": 2, "reason": "readback_unavailable"}
            return ToolResult(ok=True, success=True, verified=False, state="B",
                              hc_schema=2, reason="readback_unavailable",
                              payload=body, tool=tool, command=command)
        body = {"success": False, "verified": False, "state": "C",
                "hc_schema": 2, "error": "write_failed"}
        return ToolResult(ok=True, success=False, verified=False, state="C",
                          hc_schema=2, error="write_failed", payload=body,
                          tool=tool, command=command)

    def _inventory_payload(self, key):
        if key == "master":
            payload = _empty_master_payload()
        else:
            payload = _empty_payload(int(key.split(":")[1]))
        inserted = self.inserted.get(key)
        if inserted:
            slot = inserted["insert"]
            ref = inserted.get("ref")
            payload["plugins"][slot] = _slot(
                slot, "ok", name="Channel EQ",
                plugin_id=contract.CHANNEL_EQ_PLUGIN_ID, bypassed=False,
                ref=ref)
        return payload

    def _eq_state_payload(self, ref):
        if self.state_payload_override is not None:
            return copy.deepcopy(self.state_payload_override)
        key = self.refs.get(ref)
        if key is None:
            raise AssertionError("unknown ref %r" % (ref,))
        bands = []
        for number, name in enumerate(BANDS, start=1):
            enabled = self.enabled.get((key, name), True)
            gain = self.gains.get((key, name), 0.0)
            bands.append({
                "band": number, "name": name,
                "filter_role": "bell" if name.startswith("Peak") else "shelf",
                "frequency": _field(1000.0),
                "q": _field(1.0),
                "enabled": _field(enabled),
                "gain": _field(gain),
                "slope": _field(None),
            })
        return {
            "success": True, "verified": True, "state": "A", "hc_schema": 2,
            "operation": "logic_plugins.get_channel_eq_state_verified",
            "complete": True, "partial_reasons": [],
            "snapshot_atomic": False, "target_ref": ref,
            "target_identity": key, "plugin_enabled": _field(True),
            "plugin_bypass": _field(False), "bands": bands,
        }

    # -- public fake surface ----------------------------------------------
    def call_tool(self, tool, arguments, timeout=None):
        command = arguments["command"]
        params = arguments.get("params") or {}
        self.calls.append((tool, command, copy.deepcopy(params)))
        if tool == contract.TOOL_PROJECT and command == "export_plan":
            return self._export_plan(params)
        if tool == contract.TOOL_PROJECT and command == "export_run":
            return self._export_run(params)
        if tool == contract.TOOL_AUDIO and command == "compare_spectra":
            return self._compare(params)
        if tool == contract.TOOL_PLUGINS:
            if command == "insert_verified":
                return self._insert(params)
            if command == "set_eq_band_verified":
                return self._write_eq(params)
            if command == "get_channel_eq_state_verified":
                return self._read_eq(params)
        raise AssertionError("unexpected call: %s.%s" % (tool, command))

    def get_inventory(self, selector):
        self.inventory_reads.append(copy.deepcopy(selector))
        if self.fail_inventory:
            return ToolResult(ok=False, error="inventory unavailable")
        key = self._key_for(selector)
        if key in self.existing:
            payload = copy.deepcopy(self.existing[key])
        else:
            payload = self._inventory_payload(key)
        return ToolResult(ok=True, payload=payload)

    def close(self):
        pass

    def _export_plan(self, params):
        self.exports.append(("plan", params["output_root"]))
        if self.plan_override is not None:
            return ToolResult(ok=True, payload=copy.deepcopy(self.plan_override))
        artifact = {
            "kind": "bounce", "status": "pending",
            "verification": {"issues": [], "path_under_output_root": True,
                             "would_overwrite": False},
        }
        if self.plan_path_outside:
            artifact["verification"]["path_under_output_root"] = False
        return ToolResult(ok=True, payload={
            "schema": "logic_pro_mcp_export_plan.v1", "status": "planned",
            "projects": [{"validation_status": "valid",
                          "validation_issues": []}],
            "artifacts": [artifact],
            "unsupported_or_blocked_steps": [{"operation": "cloud_delivery"}],
        })

    def _export_run(self, params):
        output_root = params["output_root"]
        self.exports.append(("run", output_root))
        if self.run_override is not None:
            payload = (copy.deepcopy(self.run_override)
                       if isinstance(self.run_override, dict)
                       else self.run_override(output_root))
            return ToolResult(ok=True, payload=payload)
        self.run_counter += 1
        if self.run_path_outside:
            path = self.run_path_outside
        else:
            path = os.path.join(output_root,
                                "bounce-%d%s" % (self.run_counter,
                                                 self.artifact_extension))
            with open(path, "wb") as handle:
                handle.write(("bounce-%d" % self.run_counter).encode("ascii"))
            self.artifacts.append(path)
        if self.settings_sequence:
            settings = self.settings_sequence.pop(0)
        else:
            settings = self.settings
        payload = {
            "schema": "logic_pro_mcp_export_run.v1", "run_id":
                "export-run-%d" % self.run_counter,
            "status": "completed", "confirmed": True,
            "collision_policy": "fail_if_exists",
            "project_count": 1, "artifacts_total": 1,
            "artifacts_verified": 1, "artifacts_skipped": 0,
            "artifacts_uncertain": 0, "artifacts_failed": 0,
            "projects": [{"identity_verified": True, "opened": True}],
            "artifacts": [{
                "kind": "bounce", "state": "A", "verified": True,
                "bounce_fired": True, "write_attempted": True,
                "error": None, "reason": None, "path": path,
                "bounce_settings": copy.deepcopy(settings),
                "bounce_settings_read_status": self.settings_status,
                "evidence": {
                    "exists": True, "file_size_bytes": 64,
                    "duration_seconds": 10.0, "sample_rate": 44100,
                    "channel_count": 2, "silence_ratio": 0.0,
                    "peak_dbfs": -1.2, "verification_status": "verified",
                    "path": path,
                },
            }],
        }
        return ToolResult(ok=True, payload=payload)

    def _insert(self, params):
        state = self._pop_state(self.insert_state)
        key = self._key_for_params(params)
        insert = params["insert"]
        if state != "A":
            return self._state_result(contract.TOOL_PLUGINS,
                                      "insert_verified", state)
        ref = None if key == "master" else "ins_%s_%d" % (
            key.replace(":", "_"), insert)
        self.inserted[key] = {"insert": insert, "ref": ref}
        if ref:
            self.refs[ref] = key
        return self._state_result(
            contract.TOOL_PLUGINS, "insert_verified", "A",
            {"observed_slot": insert,
             "observed_plugin_id": contract.CHANNEL_EQ_PLUGIN_ID})

    def _write_eq(self, params):
        state = self._pop_state(self.write_state)
        key = self._key_for_params(params)
        band = params["band"]
        if state != "A":
            return self._state_result(contract.TOOL_PLUGINS,
                                      "set_eq_band_verified", state)
        if params.get("parameter") == "Gain":
            self.gains[(key, band)] = float(params["value"])
        return self._state_result(
            contract.TOOL_PLUGINS, "set_eq_band_verified", "A",
            {"observed_value": params.get("value")})

    def _read_eq(self, params):
        self.state_reads += 1
        ref = params["target_ref"]
        payload = self._eq_state_payload(ref)
        return self._state_result(
            contract.TOOL_PLUGINS, "get_channel_eq_state_verified", "A",
            payload)

    def _compare(self, params):
        before = params["before_path"]
        after = params["after_path"]
        self.comparison_pairs.append((before, after))
        if self.comparison_override is not None:
            payload = (copy.deepcopy(self.comparison_override)
                       if isinstance(self.comparison_override, dict)
                       else self.comparison_override(before, after))
            return ToolResult(ok=True, payload=payload)
        before_sha = sha256_file(before)
        after_sha = sha256_file(after)
        after_fp = "sha256:" + after_sha
        if self.fingerprint_override:
            after_fp = self.fingerprint_override
        bands = self.comparisons.pop(0) if self.comparisons else []
        payload = {
            "complete": True,
            "before": {"path": before,
                       "artifactFingerprint": "sha256:" + before_sha},
            "after": {"path": after, "artifactFingerprint": after_fp},
            "shapeComparison": {
                "complete": self.comparison_complete,
                "levelOffsetDb": 0.0,
                "bands": copy.deepcopy(bands),
            },
        }
        return ToolResult(ok=True, payload=payload)


# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------
def _tracks(index=TRACK):
    return {"vocal": {"index": index, "name": "Lead Vocal"}}


def _config(output_root, recipe, **overrides):
    values = {
        "project": PROJECT, "recipe": recipe, "output_root": output_root,
        "tracks": _tracks(), "master": {"name": "Stereo Out"},
        "endpoint": "http://127.0.0.1:8765/mcp", "run_id": "run-1",
        "apply": True,
    }
    values.update(overrides)
    return LoopConfig(**values)


def _tempdir():
    return tempfile.mkdtemp(prefix="loop-test-")


def _cleanup(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _run(root, recipe, bridge, **overrides):
    measure = overrides.pop("measure", None)
    connect = overrides.pop("connect", None)
    config = _config(root, recipe, **overrides)
    kwargs = {"bridge": bridge,
              # No test ever reads a real bounce; every apply gets fake metrics.
              "measure": measure or _measure_sequence([])}
    if connect is not None:
        kwargs["connect"] = connect
    return run_loop(config, **kwargs)


def _ops(report, command):
    return [entry for entry in report["operations"]
            if entry.get("operation") == "logic_plugins.%s" % command]


def _dirs(root, run_id="run-1"):
    base = os.path.join(root, run_id)
    if not os.path.isdir(base):
        return []
    return sorted(os.listdir(base))


def _assert_ok(report, reason, exit_code):
    assert report["stop_reason"] == reason, (report["stop_reason"],
                                             report.get("detail"))
    assert report["exit"] == exit_code, (report["exit"], report.get("detail"))


# ---------------------------------------------------------------------------
# preflight / dry run
# ---------------------------------------------------------------------------
def test_dry_run_prints_layout_and_writes_nothing():
    root = _tempdir()
    try:
        bridge = FakeBridge()
        report_path = os.path.join(root, "report.json")
        report = _run(root, [_bell()], bridge, apply=False, report_path=report_path)
        _assert_ok(report, "dry_run", 0)
        assert report["dry_run"] is True
        assert report["layout"]["baseline"].endswith("/run-1/baseline")
        assert "iter-1" in report["layout"] and "iter-2" in report["layout"]
        assert report["export_plan_preview"]["artifact"] == "bounce"
        assert not os.path.exists(os.path.join(root, "run-1"))
        assert not os.path.exists(report_path)
        assert [entry[0] for entry in bridge.exports] == []
        assert report["plan"]["steps"][0]["status"] == "ready"
    finally:
        _cleanup(root)


def test_before_requires_trust_before_any_export():
    root = _tempdir()
    before = os.path.join(root, "before.wav")
    with open(before, "wb") as handle:
        handle.write(b"external")
    try:
        bridge = FakeBridge()
        report = _run(root, [_bell()], bridge, before=before,
                      trust_before=False)
        _assert_ok(report, "trusted_before_required", 1)
        assert bridge.exports == []
        assert _dirs(root) == []
    finally:
        _cleanup(root)


def test_run_id_escape_is_rejected_before_writing():
    root = _tempdir()
    try:
        bridge = FakeBridge()
        report = _run(root, [_bell()], bridge, run_id="../escape")
        _assert_ok(report, "output_root_escape", 1)
        assert bridge.exports == []
        assert not os.path.exists(os.path.join(os.path.dirname(root), "escape"))
    finally:
        _cleanup(root)


def test_dry_run_with_an_invalid_run_id_reports_instead_of_crashing():
    root = _tempdir()
    try:
        bridge = FakeBridge()
        report_path = os.path.join(root, "report.json")
        report = _run(root, [_bell()], bridge, apply=False,
                      run_id="../escape", report_path=report_path)
        _assert_ok(report, "output_root_escape", 1)
        assert report["layout"] == {}
        assert bridge.exports == []
        assert not os.path.exists(report_path)
        assert not os.path.exists(os.path.join(os.path.dirname(root), "escape"))
    finally:
        _cleanup(root)


def test_dry_run_before_without_trust_reports_instead_of_crashing():
    root = _tempdir()
    before = os.path.join(root, "before.wav")
    with open(before, "wb") as handle:
        handle.write(b"external")
    try:
        bridge = FakeBridge()
        report = _run(root, [_bell()], bridge, apply=False, before=before,
                      trust_before=False)
        _assert_ok(report, "trusted_before_required", 1)
        assert report["layout"] == {}
        assert bridge.exports == []
        assert _dirs(root) == []
    finally:
        _cleanup(root)


def test_manual_only_and_terminal_stop_without_export():
    root = _tempdir()
    try:
        bridge = FakeBridge()
        report = _run(root, [_limiter()], bridge, apply=True)
        _assert_ok(report, "manual_only", 2)
        assert bridge.exports == []
        assert report["manual"][0]["action"] == "limiter"

        report = _run(root, [_send_back()], bridge, apply=True)
        _assert_ok(report, "recipe_terminal", 0)
        assert bridge.exports == []
        assert _dirs(root) == []
    finally:
        _cleanup(root)


def test_no_bridge_is_a_warning_in_dry_run_but_a_block_in_apply():
    root = _tempdir()
    try:
        def fail(endpoint):
            raise RuntimeError("connection refused")

        report = _run(root, [_bell()], None, apply=False, connect=fail)
        assert report["exit"] == 0
        assert report["stop_reason"] == "dry_run"
        assert any("cannot reach the bridge" in warning
                   for warning in report["warnings"])

        report = _run(root, [_bell()], None, apply=True, connect=fail)
        _assert_ok(report, "plan_blocked", 1)
        assert _dirs(root) == []
    finally:
        _cleanup(root)


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------
def test_track_bell_converges_and_reports_hashes_and_windows():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "tolerance", 0)
        assert _dirs(root) == ["baseline", "iter-1"]
        assert report["baseline_source"] == "exported"
        assert len(report["iterations"]) == 1
        entry = report["iterations"][0]
        artifact = entry["artifact"]
        assert artifact.endswith("/run-1/iter-1/bounce-2.wav")
        assert entry["sha256"] == sha256_file(artifact)
        assert entry["comparison"]["after_fingerprint"] == \
            "sha256:" + entry["sha256"]
        bell = entry["bells"][0]
        assert bell["G_cur"] == -2.0
        assert bell["effect_incremental_db"] == -2.0
        assert bell["effect_overall_db"] == -2.0
        assert bell["overall_deviation_db"] == 0.0
        assert bell["window_octaves"] == 0.36
        assert bell["readback"]["observed_db"] == -2.0
        assert bell["disposition"] == "landed"
        assert report["plan"]["summary"]["calls"] == 4
        assert len(_ops(report, "insert_verified")) == 1
        assert len(_ops(report, "set_eq_band_verified")) == 3
    finally:
        _cleanup(root)


def test_static_shelf_creates_stage_and_bells_compare_against_it():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_shelf(), _bell()], bridge)
        _assert_ok(report, "tolerance", 0)
        assert _dirs(root) == ["baseline", "iter-1", "stage"]
        assert report["stage"] is not None
        stage_path = report["stage"]["path"]
        before, after = bridge.comparison_pairs[0]
        assert before == stage_path
        assert after == report["iterations"][0]["artifact"]
        assert len(_ops(report, "insert_verified")) == 1
        assert len(report["iterations"][0]["bells"]) == 1
    finally:
        _cleanup(root)


def test_second_iteration_corrects_and_converges():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[
            _bands(-1.0, 800),   # iter-1: base -> iter-1
            _bands(-1.5, 800),   # iter-2 incremental: iter-1 -> iter-2
            _bands(-2.5, 800),   # iter-2 overall: base -> iter-2
        ])
        report = _run(root, [_bell()], bridge, tolerance_db=0.75,
                      incremental_tolerance_db=0.5)
        _assert_ok(report, "tolerance", 0)
        assert len(report["iterations"]) == 2
        second = report["iterations"][1]
        bell = second["bells"][0]
        assert bell["G_prev"] == -2.0
        assert bell["G_cur"] == -3.0
        assert bell["delta_db"] == -1.0
        assert bell["overall_deviation_db"] == -0.5
        correction_writes = [
            entry for entry in _ops(report, "set_eq_band_verified")
            if entry.get("payload", {}).get("observed_value") == -3.0]
        assert len(correction_writes) == 1
        assert _dirs(root) == ["baseline", "iter-1", "iter-2"]
    finally:
        _cleanup(root)


def test_trusted_before_is_copied_under_run_root():
    root = _tempdir()
    before = os.path.join(root, "before.wav")
    with open(before, "wb") as handle:
        handle.write(b"external-baseline")
    try:
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge, before=before,
                      trust_before=True)
        _assert_ok(report, "tolerance", 0)
        assert report["baseline_source"] == "trusted_external"
        baseline = report["baseline"]
        assert baseline["path"].startswith(
            os.path.join(os.path.realpath(root), "run-1", "baseline"))
        assert os.path.basename(baseline["path"]) == "external-before.wav"
        assert baseline["origin_path"] == os.path.abspath(before)
        assert "baseline_provenance_unverified" in report["warnings"]
        assert len(bridge.exports) == 2  # plan+run for iter-1 only
        assert [entry[0] for entry in bridge.exports] == ["plan", "run"]
        assert [name for name in _dirs(root)] == ["baseline", "iter-1"]
    finally:
        _cleanup(root)


# ---------------------------------------------------------------------------
# corrections and honest stops
# ---------------------------------------------------------------------------
def test_master_bell_is_advisory_and_never_corrected():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-1.0, 800)])
        report = _run(root, [_bell(target="master", gain=-3.0)], bridge)
        _assert_ok(report, "master_eq_advisory", 2)
        assert report["iterations"][0]["bells"][0]["disposition"] == "advisory"
        writes = _ops(report, "set_eq_band_verified")
        assert len(writes) == 3
        params = [call[2] for call in bridge.calls
                  if call[1] == "set_eq_band_verified"]
        assert all("strip" in item and "track" not in item for item in params)
        assert all("target_ref" not in item for item in params)
        inventory = bridge.inventory_reads[-1]
        assert inventory == {"strip": "output"}
        assert report["warnings"].count("band_enablement_unverified") == 1
    finally:
        _cleanup(root)


def test_manual_dynamics_blocks_correction_and_marks_partial_manual():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-1.0, 800)])
        report = _run(root, [_limiter(), _bell(gain=-3.0)], bridge)
        _assert_ok(report, "residual_low_trust", 2)
        assert len(_ops(report, "insert_verified")) == 1
        assert "manual_actions_pending" in report["warnings"]
        assert not any(call[1] == "insert_verified" and
                       call[2].get("plugin") == "Limiter"
                       for call in bridge.calls)
    finally:
        _cleanup(root)


def test_state_b_insert_stops_before_the_iteration_export():
    root = _tempdir()
    try:
        bridge = FakeBridge(insert_state="B")
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "state_not_verified", 1)
        assert report["iterations"] == []
        assert _dirs(root) == ["baseline"]
        assert [entry[0] for entry in bridge.exports] == ["plan", "run"]
    finally:
        _cleanup(root)


def test_disabled_band_is_not_written():
    root = _tempdir()
    try:
        bridge = FakeBridge(
            existing={"track:3": _existing_track_payload()},
            enabled={("track:3", "Peak 1"): False},
            comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge, overwrite_eq=True)
        _assert_ok(report, "band_enablement_unverified", 2)
        assert _ops(report, "set_eq_band_verified") == []
        assert _ops(report, "insert_verified") == []
        assert report["iterations"] == []
    finally:
        _cleanup(root)


def test_readback_confirmed_when_the_effect_is_too_small():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-0.1, 800)])
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "readback_confirmed", 0)
        bell = report["iterations"][0]["bells"][0]
        assert bell["disposition"] == "no_measurable_effect"
        assert bell["readback"]["ok"] is True
    finally:
        _cleanup(root)


def test_sign_mismatch_stops_without_correcting():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(1.5, 800)])
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "sign_mismatch", 2)
        assert len(report["iterations"][0]["bells"]) == 1
        assert report["iterations"][0]["bells"][0]["disposition"] == \
            "sign_mismatch"
        assert len(_ops(report, "set_eq_band_verified")) == 3
    finally:
        _cleanup(root)


def test_correction_cap_refuses_a_gain_beyond_gate():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-1.0, 800)])
        report = _run(root, [_bell(gain=-2.5)], bridge, tolerance_db=0.5,
                      incremental_tolerance_db=0.5)
        _assert_ok(report, "correction_cap", 2)
        assert len(_ops(report, "set_eq_band_verified")) == 3
        assert "correction" in json.dumps(report)
    finally:
        _cleanup(root)


def test_residual_unavailable_is_not_interpolated():
    root = _tempdir()
    try:
        bands = [{"centerHz": 800, "residualDb": None,
                  "unavailableReason": "band_masked"}]
        bridge = FakeBridge(comparisons=[bands])
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "residual_unavailable", 2)
        bell = report["iterations"][0]["bells"][0]
        assert bell["effect_incremental_db"] is None
        assert bell["disposition"] == "residual_unavailable"
    finally:
        _cleanup(root)


# ---------------------------------------------------------------------------
# export / artifact / guard failures
# ---------------------------------------------------------------------------
def test_export_run_must_complete():
    root = _tempdir()
    try:
        bridge = FakeBridge(run_override={
            "schema": "logic_pro_mcp_export_run.v1", "status": "failed",
            "confirmed": True})
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "export_failed", 1)
        assert report["iterations"] == []
        assert _dirs(root) == ["baseline"]
    finally:
        _cleanup(root)


def test_artifact_outside_the_iteration_directory_is_rejected():
    root = _tempdir()
    outside = os.path.join(root, "outside.wav")
    try:
        bridge = FakeBridge(run_path_outside=outside)
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "output_root_escape", 1)
        assert report["iterations"] == []
    finally:
        _cleanup(root)


def test_fingerprint_mismatch_is_artifact_changed():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800)],
                            fingerprint_override="sha256:deadbeef")
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "artifact_changed", 1)
        assert len(report["iterations"]) == 0
    finally:
        _cleanup(root)


def test_bounce_settings_must_not_change_between_runs():
    root = _tempdir()
    try:
        bridge = FakeBridge(
            settings_sequence=[
                {"format": "wav", "bit_depth": 24, "sample_rate": 44100},
                {"format": "wav", "bit_depth": 16, "sample_rate": 44100},
            ],
            comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "bounce_settings_changed", 1)
        assert report["iterations"] == []
    finally:
        _cleanup(root)


def test_clipping_guardrail_stops_after_the_export():
    root = _tempdir()
    try:
        measure = _measure_sequence([
            _metrics(),
            _metrics(clipping={"count": 3}),
        ])
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge, measure=measure)
        _assert_ok(report, "guardrail_failed", 1)
        # The rejected iteration never becomes a completed iteration, but the
        # bounce and its measurements must survive in the report.
        assert report["iterations"] == []
        rejected = report["exports"][-1]
        assert rejected["label"] == "iter-1"
        assert rejected["metrics"]["clipping"]["count"] == 3
        assert rejected["guards"]["guardrails"] == ["clipping increased"]
    finally:
        _cleanup(root)


def test_sample_rate_mismatch_is_artifact_mismatch():
    root = _tempdir()
    try:
        measure = _measure_sequence([
            _metrics(),
            _metrics(sample_rate=48000),
        ])
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge, measure=measure)
        _assert_ok(report, "artifact_mismatch", 1)
        assert report["iterations"] == []
    finally:
        _cleanup(root)


def test_alternate_export_extension_is_accepted():
    root = _tempdir()
    try:
        bridge = FakeBridge(artifact_extension=".aif",
                            comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "tolerance", 0)
        assert report["iterations"][0]["artifact"].endswith(".aif")
    finally:
        _cleanup(root)


def test_static_only_stops_after_the_stage_bounce():
    root = _tempdir()
    try:
        bridge = FakeBridge()
        report = _run(root, [_shelf()], bridge)
        _assert_ok(report, "static_only", 0)
        assert report["stage"] is not None
        assert _dirs(root) == ["baseline", "stage"]
        assert report["iterations"] == []
        assert bridge.comparison_pairs == []
        assert len(_ops(report, "insert_verified")) == 1
    finally:
        _cleanup(root)


def test_max_iterations_stops_before_writing_a_correction():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-1.0, 800)])
        report = _run(root, [_bell()], bridge, max_iterations=1,
                      tolerance_db=0.5, incremental_tolerance_db=0.5)
        _assert_ok(report, "max_iterations", 2)
        assert len(report["iterations"]) == 1
        assert len(_ops(report, "set_eq_band_verified")) == 3
        assert report["iterations"][0]["bells"][0]["disposition"] == "correction"
    finally:
        _cleanup(root)


def test_no_improvement_stops_before_correcting_again():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[
            _bands(-1.0, 800),    # iter-1 incremental
            _bands(-0.25, 800),   # iter-2 incremental: only 25% transferred
            _bands(-1.25, 800),   # iter-2 overall
        ])
        report = _run(root, [_bell()], bridge, tolerance_db=0.75,
                      incremental_tolerance_db=0.5, no_improvement_db=0.3)
        _assert_ok(report, "no_improvement", 2)
        assert len(report["iterations"]) == 2
        corrections = [entry for entry in _ops(report, "set_eq_band_verified")
                       if entry.get("payload", {}).get("observed_value") == -3.0]
        assert len(corrections) == 1
    finally:
        _cleanup(root)


def test_effect_mismatch_when_every_increment_lands_but_the_sum_does_not():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[
            _bands(-1.0, 800),    # iter-1 incremental
            _bands(-1.0, 800),    # iter-2 incremental: landed
            _bands(-1.5, 800),    # iter-2 overall: does not match G_cur
        ])
        report = _run(root, [_bell()], bridge, tolerance_db=0.5,
                      incremental_tolerance_db=0.5)
        _assert_ok(report, "effect_mismatch", 2)
        second = report["iterations"][1]["bells"][0]
        assert second["disposition"] == "landed"
        assert second["overall_deviation_db"] == -1.5
        assert len(_ops(report, "set_eq_band_verified")) == 4
    finally:
        _cleanup(root)


def test_overlapping_bell_windows_are_never_corrected():
    root = _tempdir()
    try:
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800, 900)])
        report = _run(root, [_bell(freq=800), _bell(freq=900)], bridge)
        _assert_ok(report, "residual_low_trust", 2)
        bells = report["iterations"][0]["bells"]
        assert [item["disposition"] for item in bells] == [
            "residual_low_trust", "residual_low_trust"]
    finally:
        _cleanup(root)


def test_bounce_settings_unverified_requires_the_explicit_flag():
    root = _tempdir()
    try:
        bridge = FakeBridge(settings_status=None,
                            comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge)
        _assert_ok(report, "bounce_settings_unverified", 1)
        assert report["iterations"] == []

        bridge = FakeBridge(settings_status=None,
                            comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge, run_id="run-2",
                      allow_unverified_bounce_settings=True)
        _assert_ok(report, "tolerance", 0)
        assert any("bounce settings unverified" in warning
                   for warning in report["warnings"])
    finally:
        _cleanup(root)


def test_unreadable_artifact_stops_the_loop():
    root = _tempdir()
    try:
        def fail(path):
            raise ValueError("unsupported sample format")

        bridge = FakeBridge()
        report = _run(root, [_bell()], bridge, measure=fail)
        _assert_ok(report, "artifact_unreadable", 1)
        assert report["iterations"] == []
    finally:
        _cleanup(root)


def test_apply_blocks_an_unresolved_target_before_exporting():
    root = _tempdir()
    try:
        bridge = FakeBridge()
        report = _run(root, [_bell()], bridge, tracks={}, master=None)
        _assert_ok(report, "plan_blocked", 1)
        assert bridge.exports == []
        assert _dirs(root) == []
    finally:
        _cleanup(root)


def test_report_is_written_only_with_apply():
    root = _tempdir()
    try:
        report_path = os.path.join(root, "report.json")
        bridge = FakeBridge(comparisons=[_bands(-2.0, 800)])
        report = _run(root, [_bell()], bridge, report_path=report_path)
        _assert_ok(report, "tolerance", 0)
        assert os.path.isfile(report_path)
        assert report["report_path"] == report_path
        with open(report_path, encoding="utf-8") as handle:
            stored = json.load(handle)
        assert stored["schema"] == "mcp_sound_engineer.loop.v1"
        assert stored["stop_reason"] == "tolerance"
        assert stored["iterations"][0]["bells"][0]["band_name"] == "Peak 1"
        assert stored["baseline"]["sha256"]
        assert stored["iterations"][0]["comparison"]["after_sha256"]
    finally:
        _cleanup(root)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ok  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                traceback.print_exc()
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} fallo(s)")
    sys.exit(1 if failures else 0)
