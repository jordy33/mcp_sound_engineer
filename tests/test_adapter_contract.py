"""Pin the adapter's emitted surface to mcp_logic HEAD b42cdd37.

The fixture records where each claim was read.  These tests are deliberately
strict: if mcp_logic adds a parameter or changes a name, the adapter must be
re-grounded here before it can emit the new shape.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adapter import mcp_logic_contract as contract  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "mcp_logic_contract.json")


def _fixture():
    with open(FIXTURE) as handle:
        return json.load(handle)


def _plugins(command, **params):
    return {"tool": contract.TOOL_PLUGINS,
            "arguments": {"command": command, "params": params}}


def _eq_params(**overrides):
    params = {"track": 3, "insert": 0, "band": "Peak 1", "parameter": "Gain",
              "value": -2.0, "unit": "dB", "mode": contract.MODE,
              "project_expected_path": "/tmp/session.logicx"}
    params.update(overrides)
    return params


def _valid_track_insert(**overrides):
    params = {"track": 3, "insert": 0, "plugin": "Channel EQ",
              "expected_name": "Lead Vocal", "mode": contract.MODE,
              "project_expected_path": "/tmp/session.logicx"}
    params.update(overrides)
    return params


def _invalid(call, needle=None):
    try:
        contract.validate_call(call)
    except contract.ContractViolation as exc:
        if needle is not None:
            assert needle in str(exc), (needle, str(exc))
        return
    raise AssertionError("expected ContractViolation: %r" % (call,))


def test_contract_matches_fixture():
    fixture = _fixture()
    assert contract.ADAPTER_CONTRACT_VERSION == fixture["contract_version"]
    assert contract.MCP_LOGIC_COMMIT == fixture["mcp_logic_commit"]
    assert contract.CONTRACT_CAPTURED_AT == fixture["captured_at"]
    assert contract.MASTER_SELECTOR == fixture["master_selector"]
    assert contract.MASTER_EXPECTED_NAME == fixture["master_expected_name"]
    assert contract.MODE == fixture["mode"]
    assert contract.HC_V2_SCHEMA == fixture["hc_v2_schema"]
    assert list(contract.EQ_VERIFIED_BANDS) == fixture["eq_verified_bands"]
    assert list(contract.EQ_OBSERVED_ONLY_BANDS) == fixture["eq_observed_only_bands"]
    assert list(contract.BELL_BANDS) == fixture["bell_bands"]
    assert list(contract.SHELF_BANDS) == fixture["shelf_bands"]
    assert {key: list(value) for key, value in contract.EQ_PARAMETER.items()} == \
        fixture["eq_parameter"]
    assert contract.CHANNEL_EQ_PLUGIN_ID == fixture["channel_eq_plugin_id"]
    assert sorted(contract.INSERTABLE_PLUGINS) == fixture["insertable_plugins"]
    assert sorted(contract.INSERTABLE_PLUGIN_IDS) == fixture["insertable_plugin_ids"]
    assert sorted(contract.COMPRESSOR_VERIFIED_PARAMS) == \
        fixture["compressor_verified_params"]
    assert sorted(contract.MANUAL_ACTIONS) == fixture["manual_actions"]
    assert sorted(contract.TERMINAL_ACTIONS) == fixture["terminal_actions"]
    assert list(contract.FORBIDDEN_EMISSIONS) == fixture["forbidden_emissions"]


def test_eq_parameter_names_are_verified():
    assert contract.EQ_PARAMETER["q"] == ("Q", "Q")
    assert contract.EQ_UNIT_BY_PARAMETER == {
        "Frequency": "Hz", "Gain": "dB", "Q": "Q"}
    assert len(contract.EQ_VERIFIED_BANDS) == 6
    assert len(contract.EQ_VERIFIED_BANDS) * 3 == 18
    assert set(contract.BELL_BANDS) | set(contract.SHELF_BANDS) == \
        set(contract.EQ_VERIFIED_BANDS)
    assert "Q-Factor" not in contract.EQ_PARAMETER_NAMES


def test_master_selector_is_output_strip_only():
    contract.validate_call(_plugins("insert_verified", **{
        "strip": "output", "insert": 2, "plugin": "Channel EQ",
        "expected_name": contract.MASTER_EXPECTED_NAME,
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}))
    contract.validate_call(_plugins(
        "insert_verified",
        strip="output", insert=2, plugin="Channel EQ",
        expected_name=contract.MASTER_EXPECTED_NAME, mode=contract.MODE,
        project_expected_path="/tmp/session.logicx"))
    _invalid(_plugins("insert_verified", **{
        "strip": "aux", "insert": 2, "plugin": "Channel EQ",
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}),
        "only the master Stereo Output strip")
    _invalid(_plugins("insert_verified", **{
        "strip": "output", "track": 0, "insert": 2, "plugin": "Channel EQ",
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}),
        "exactly one selector")
    _invalid(_plugins("insert_verified", **{
        "strip": "output", "target_ref": "ins_abc", "insert": 2,
        "plugin": "Channel EQ", "mode": contract.MODE,
        "project_expected_path": "/tmp/session.logicx"}), "cannot be combined with strip")


def test_insert_allowlist_excludes_limiter():
    assert "Limiter" not in contract.INSERTABLE_PLUGINS
    assert contract.LIMITER_PLUGIN_ID not in contract.INSERTABLE_PLUGIN_IDS
    _invalid(_plugins("insert_verified", **{
        "track": 3, "insert": 4, "plugin": "Limiter", "expected_name": "Vox",
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}),
        "not insertable")
    for plugin in ("Gain", "Channel EQ", "Compressor"):
        contract.validate_call(_plugins("insert_verified", **{
            "track": 3, "insert": 4, "plugin": plugin, "expected_name": "Vox",
            "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}))


def test_canonical_plugin_ids_are_insertable():
    for plugin_id in contract.INSERTABLE_PLUGIN_IDS:
        contract.validate_call(_plugins("insert_verified", **{
            "track": 3, "insert": 4, "plugin": plugin_id, "expected_name": "Vox",
            "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}))


def test_track_insert_requires_corroboration():
    _invalid(_plugins("insert_verified", **{
        "track": 3, "insert": 4, "plugin": "Channel EQ",
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}),
        "expected_name or target_ref")
    contract.validate_call(_plugins("insert_verified", **{
        "track": 3, "insert": 4, "plugin": "Channel EQ", "target_ref": "ins_abc",
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}))


def test_set_eq_band_requires_verified_name_unit_and_band():
    contract.validate_call(_plugins("set_eq_band_verified", **_eq_params()))
    contract.validate_call(_plugins("set_eq_band_verified", **_eq_params(
        parameter="Q", unit="Q", value=1.2)))
    _invalid(_plugins("set_eq_band_verified", **_eq_params(parameter="Q-Factor")),
             "forbidden emission")
    _invalid(_plugins("set_eq_band_verified", **_eq_params(unit="Q")),
             "requires unit 'dB'")
    _invalid(_plugins("set_eq_band_verified", **_eq_params(band="Low Cut")),
             "observed-only")
    _invalid(_plugins("set_eq_band_verified", **_eq_params(parameter="Gain",
                                                           unit=None)),
             "requires unit 'dB'")
    _invalid(_plugins("set_eq_band_verified", **_eq_params(expected_name="Lead")),
             "does not accept expected_name")


def test_set_eq_band_accepts_master_strip():
    params = _eq_params(strip="output")
    params.pop("track")
    contract.validate_call(_plugins("set_eq_band_verified", **params))


def test_set_eq_band_accepts_plugin_insert_ref():
    params = _eq_params(target_ref="ins_abc")
    params.pop("track")
    params.pop("insert")
    contract.validate_call(_plugins("set_eq_band_verified", **params))
    # The ref may corroborate an explicit track/insert pair.
    params["track"] = 3
    params["insert"] = 2
    contract.validate_call(_plugins("set_eq_band_verified", **params))
    _invalid(_plugins("set_eq_band_verified", **dict(
        _eq_params(target_ref=""), insert=None)), "non-empty plugin-insert reference")
    bad = _eq_params(strip="output", target_ref="ins_abc")
    bad.pop("track")
    _invalid(_plugins("set_eq_band_verified", **bad), "cannot be combined with strip")


def test_insert_verified_accepts_target_ref_without_track_or_insert():
    contract.validate_call(_plugins("insert_verified", **{
        "target_ref": "ins_abc", "plugin": "Channel EQ",
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}))
    _invalid(_plugins("insert_verified", **{
        "target_ref": "ins_abc", "plugin": "Channel EQ", "expected_name": "Lead",
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}),
        "does not accept expected_name with target_ref")
    _invalid(_plugins("insert_verified", **{
        "strip": "output", "plugin": "Channel EQ", "insert": 0,
        "mode": contract.MODE, "project_expected_path": "/tmp/session.logicx"}),
        "requires expected_name")


def test_set_param_verified_accepts_target_ref():
    contract.validate_call(_plugins("set_param_verified", **{
        "target_ref": "ins_abc", "plugin": "Compressor", "param": "threshold",
        "value": 50, "unit": "percent", "mode": contract.MODE,
        "project_expected_path": "/tmp/session.logicx"}))


def test_forbidden_parameter_names_are_rejected():
    for name in ("ratio", "attack", "release"):
        _invalid(_plugins("set_param_verified", **{
            "track": 3, "insert": 0, "plugin": "Compressor", "param": name,
            "value": 1, "unit": "boolean", "mode": contract.MODE,
            "project_expected_path": "/tmp/session.logicx"}),
            "forbidden emission")
    for name in ("phaseInvert", "outputCeiling"):
        _invalid(_plugins("set_param_verified", **{
            "track": 3, "insert": 0, "plugin": "Gain", "param": name,
            "value": 1, "unit": "boolean", "mode": contract.MODE,
            "project_expected_path": "/tmp/session.logicx"}),
            "forbidden emission")
    _invalid(_plugins("set_param_verified", **{
        "track": 3, "insert": 0, "plugin": "Gain", "param": "gain",
        "value": 1, "unit": "dB", "mode": contract.MODE,
        "project_expected_path": "/tmp/session.logicx"}),
        "only the Compressor")


def test_compressor_verified_parameters_are_accepted():
    for name, unit in (("threshold", "percent"), ("limiter_on", "boolean"),
                       ("auto_release", "boolean")):
        contract.validate_call(_plugins("set_param_verified", **{
            "track": 3, "insert": 0, "plugin": "Compressor", "param": name,
            "value": 1, "unit": unit, "mode": contract.MODE,
            "project_expected_path": "/tmp/session.logicx"}))


def test_plugin_ref_reads_reject_strip():
    for command in sorted(contract.PLUGIN_REF_READ_COMMANDS):
        contract.validate_call(_plugins(command, target_ref="ins_abc"))
        _invalid(_plugins(command, strip="output"), "rejects strip")
        _invalid(_plugins(command, target_ref=""), "non-empty")


def test_hc_v2_is_plugins_only():
    assert contract.HC_V2_TOOLS == frozenset({contract.TOOL_PLUGINS})
    assert contract.HC_V2_SCHEMA == 2


def test_mixer_calls_are_track_bound():
    for command in ("set_volume", "set_pan"):
        call = {"tool": contract.TOOL_MIXER,
                "arguments": {"command": command,
                              "params": {"track": 3, "value": 0.5}}}
        contract.validate_call(call)
    _invalid({"tool": contract.TOOL_MIXER,
              "arguments": {"command": "set_volume",
                            "params": {"track": -1, "value": 0.5}}})
    _invalid({"tool": contract.TOOL_MIXER,
              "arguments": {"command": "set_volume",
                            "params": {"strip": "output", "value": 0.5}}})


def test_export_shapes():
    base = {"output_root": "/tmp/exports", "projects": ["/tmp/session.logicx"],
            "collision_policy": "fail_if_exists", "artifact": "bounce"}
    contract.validate_call({"tool": contract.TOOL_PROJECT,
                            "arguments": {"command": "export_plan", "params": base}})
    run = dict(base)
    run["confirmed"] = True
    contract.validate_call({"tool": contract.TOOL_PROJECT,
                            "arguments": {"command": "export_run", "params": run}})
    _invalid({"tool": contract.TOOL_PROJECT,
              "arguments": {"command": "export_plan",
                            "params": dict(base, confirmed=True)}},
             "does not accept confirmed")
    _invalid({"tool": contract.TOOL_PROJECT,
              "arguments": {"command": "export_run", "params": base}},
             "boolean confirmed")
    _invalid({"tool": contract.TOOL_PROJECT,
              "arguments": {"command": "export_plan",
                            "params": dict(base, collision_policy="overwrite")}},
             "collision_policy")


def test_audio_shapes():
    contract.validate_call({"tool": contract.TOOL_AUDIO,
                            "arguments": {"command": "analyze_file",
                                          "params": {"path": "/tmp/a.wav"}}})
    compare = {"before_path": "/tmp/a.wav", "after_path": "/tmp/b.wav",
               "output_root": "/tmp/exports",
               "comparison_mode": "spectral_shape"}
    contract.validate_call({"tool": contract.TOOL_AUDIO,
                            "arguments": {"command": "compare_spectra",
                                          "params": compare}})
    _invalid({"tool": contract.TOOL_AUDIO,
              "arguments": {"command": "compare_spectra",
                            "params": dict(compare, comparison_mode="delta")}},
             "comparison_mode")
    _invalid({"tool": contract.TOOL_AUDIO,
              "arguments": {"command": "analyze_file",
                            "params": {"path": "relative.wav"}}},
             "absolute path")


def test_unknown_tool_and_command_are_rejected():
    _invalid({"tool": "logic_bounce", "arguments": {"command": "go", "params": {}}},
             "unknown tool")
    _invalid(_plugins("delete_plugin"), "not supported")
    _invalid(_plugins("set_eq_band_verified", **_eq_params(mode="confirmed_live")),
             "mode must be")


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
