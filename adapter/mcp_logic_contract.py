"""The mcp_logic surface this adapter is allowed to emit, pinned to source.

Everything here was read from mcp_logic HEAD ``b42cdd37`` on 2026-10-26.  The
provenance for each claim is recorded in ``tests/fixtures/mcp_logic_contract.json``
(file + line numbers).  When mcp_logic changes, update the fixture and this
module together; the contract tests fail if they drift apart.

The important corrections relative to the previous adapter:

* Channel EQ ``Q`` is the literal parameter name ``"Q"``, not ``"Q-Factor"``.
* Only the six named non-cut bands (18 parameters) have a measured write +
  readback round trip.  Low Cut / High Cut are observed-only.
* ``insert_verified``'s real allowlist is Gain, Channel EQ and Compressor.
  Limiter is documented in the tool text but absent from
  ``insertableAllowlist``, so it is refused with ``unknown_plugin_identity``.
* The master Stereo Output strip has no Arrange index: use ``strip: "output"``.
  It emits no ``plugin_insert_ref``, so the master EQ state cannot be read back.
* ``logic_plugins.*`` uses Honest Contract v2 (``hc_schema: 2``).
"""
from __future__ import annotations

import os

ADAPTER_CONTRACT_VERSION = "mcp_logic@b42cdd37+2026-10-26"
MCP_LOGIC_COMMIT = "b42cdd37"
CONTRACT_CAPTURED_AT = "2026-10-26"

# --------------------------------------------------------------------------
# tool routing: every mcp_logic tool is a command tool
# --------------------------------------------------------------------------
TOOL_PLUGINS = "logic_plugins"
TOOL_MIXER = "logic_mixer"
TOOL_PROJECT = "logic_project"
TOOL_AUDIO = "logic_audio"

TOOL_COMMANDS = {
    TOOL_PLUGINS: frozenset({
        "get_inventory",
        "get_param_verified",
        "get_channel_eq_state_verified",
        "plan_parameter_changes",
        "set_param_verified",
        "set_eq_band_verified",
        "insert_verified",
    }),
    TOOL_MIXER: frozenset({"set_volume", "set_pan"}),
    TOOL_PROJECT: frozenset({"export_plan", "export_run"}),
    TOOL_AUDIO: frozenset({"analyze_file", "compare_spectra"}),
}

# Tools whose arguments are Honest Contract v2 envelopes.
HC_V2_TOOLS = frozenset({TOOL_PLUGINS})
HC_V2_SCHEMA = 2

# --------------------------------------------------------------------------
# addressing
# --------------------------------------------------------------------------
MODE = "duplicate_applyback"
MASTER_STRIP = "output"
MASTER_SELECTOR = {"strip": MASTER_STRIP}
MASTER_EXPECTED_NAME = "Stereo Out"  # measured name; runtime locates the strip by it

# Writable plugin commands accept a track index, a plugin-insert target_ref, or
# strip:"output".  target_ref may corroborate an explicit track/insert, but never
# a strip.  When target_ref is supplied, `insert` may be omitted (the ref binds
# it); the explicit-index path still requires it.
PLUGIN_WRITE_COMMANDS = frozenset({
    "insert_verified", "set_param_verified", "set_eq_band_verified",
})
# These reads require a plugin-insert target_ref (ins_...) and reject strip.
PLUGIN_REF_READ_COMMANDS = frozenset({
    "get_param_verified", "get_channel_eq_state_verified", "plan_parameter_changes",
})
TRACK_PLUGIN_TOOLS = tuple(sorted(PLUGIN_WRITE_COMMANDS))

# --------------------------------------------------------------------------
# Channel EQ
# --------------------------------------------------------------------------
# 18 verified parameters = Frequency/Gain/Q on these six named bands.
EQ_VERIFIED_BANDS = (
    "Low Shelf", "Peak 1", "Peak 2", "Peak 3", "Peak 4", "High Shelf",
)
EQ_OBSERVED_ONLY_BANDS = ("Low Cut", "High Cut")
BELL_BANDS = ("Peak 1", "Peak 2", "Peak 3", "Peak 4")
SHELF_BANDS = ("Low Shelf", "High Shelf")

# recipe key -> (mcp_logic parameter name, required unit)
EQ_PARAMETER = {
    "freq": ("Frequency", "Hz"),
    "gain": ("Gain", "dB"),
    "q": ("Q", "Q"),
}
EQ_UNIT_BY_PARAMETER = {name: unit for name, unit in EQ_PARAMETER.values()}
EQ_PARAMETER_NAMES = frozenset(EQ_UNIT_BY_PARAMETER)

# --------------------------------------------------------------------------
# plugins
# --------------------------------------------------------------------------
CHANNEL_EQ_PLUGIN_ID = "logic.stock.effect.channel_eq"
INSERTABLE_PLUGINS = frozenset({"Gain", "Channel EQ", "Compressor"})
INSERTABLE_PLUGIN_IDS = frozenset({
    "logic.stock.effect.gain",
    "logic.stock.effect.channel_eq",
    "logic.stock.effect.compressor",
})
COMPRESSOR_PLUGIN_ID = "logic.stock.effect.compressor"
# The only Compressor parameters with a measured write + readback.
COMPRESSOR_VERIFIED_PARAMS = frozenset({"threshold", "limiter_on", "auto_release"})
# Not insertable at all, despite the tool description mentioning it.
LIMITER_PLUGIN_ID = "logic.stock.effect.limiter"

# --------------------------------------------------------------------------
# action capability matrix
# --------------------------------------------------------------------------
READY_ACTIONS = frozenset({"fader", "pan", "eq.bell", "eq.shelf"})
MANUAL_ACTIONS = frozenset({
    "eq.high_pass",
    "eq.high_pass_stem",
    "compressor",
    "limiter",
    "polarity.invert",
    "de_esser",
    "dither",
    "stereo.width",
    "dynamic_eq",
})
TERMINAL_ACTIONS = frozenset({"send_back"})

MANUAL_REASONS = {
    "limiter": ("Limiter is not in mcp_logic's insert allowlist (despite the "
                "tool text naming it) and has no verified-writable parameter "
                "(ceiling included)"),
    "compressor": ("Compressor verifies only threshold, limiter_on and auto_release; "
                   "ratio/attack/release have no verified readback"),
    "polarity.invert": ("Gain is insert-only; phaseInvert has no verified write "
                        "or readback"),
    "eq.high_pass": ("Low Cut is observed-only; mcp_logic has no verified Low Cut "
                     "write"),
    "eq.high_pass_stem": ("Low Cut is observed-only; mcp_logic has no verified Low "
                          "Cut write"),
    "de_esser": "no verified De-Esser surface in mcp_logic",
    "dither": "dither is a bounce setting, not a plug-in parameter",
    "stereo.width": "no verified stereo-imaging parameter in mcp_logic",
    "dynamic_eq": "no verified dynamic-EQ surface in mcp_logic",
}

# Names the old adapter emitted that the verified surface rejects or never
# verifies.  They must never appear in a generated call's parameter/plugin/band
# fields (see validate_call).
FORBIDDEN_EMISSIONS = (
    "Q-Factor",
    "outputCeiling",
    "phaseInvert",
    "ratio",
    "attack",
    "release",
)
FORBIDDEN_PARAMETER_NAMES = frozenset(FORBIDDEN_EMISSIONS)
FORBIDDEN_PLUGIN_NAMES = frozenset({"Limiter"})

EXPORT_ARTIFACT_KINDS = frozenset({"bounce", "stem", "preview", "variant"})
COLLISION_POLICIES = frozenset({"fail_if_exists", "skip_existing"})
COMPARISON_MODES = frozenset({"absolute", "spectral_shape"})


class ContractViolation(ValueError):
    """A generated call is outside the pinned verified surface."""


def _fail(message: str) -> None:
    raise ContractViolation(message)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_absolute_path(value) -> bool:
    return isinstance(value, str) and value.startswith("/")


def _selector(params: dict) -> str:
    """Return "track" or "output"; reject missing or duplicate selectors."""
    has_track = "track" in params
    has_strip = "strip" in params
    if has_track and has_strip:
        _fail("both 'track' and strip were supplied; exactly one selector is accepted")
    if not has_track and not has_strip:
        _fail("missing selector: supply 'track' or strip:'output'")
    if has_strip:
        if params["strip"] != MASTER_STRIP:
            _fail("strip must be %r; only the master Stereo Output strip is supported"
                  % MASTER_STRIP)
        if "target_ref" in params:
            _fail("target_ref addresses an insert; it cannot be combined with strip")
        return "output"
    track = params["track"]
    if not _is_int(track) or track < 0:
        _fail("'track' must be an Int >= 0")
    return "track"


def _write_selector(params: dict) -> str:
    """Return "track", "ref" or "output" for a verified plugin write."""
    has_track = "track" in params
    has_strip = "strip" in params
    has_ref = "target_ref" in params
    if has_strip:
        if has_track:
            _fail("both 'track' and strip were supplied; exactly one selector is accepted")
        if has_ref:
            _fail("target_ref addresses an insert; it cannot be combined with strip")
        if params["strip"] != MASTER_STRIP:
            _fail("strip must be %r; only the master Stereo Output strip is supported"
                  % MASTER_STRIP)
        return "output"
    if has_ref:
        ref = params["target_ref"]
        if not isinstance(ref, str) or not ref:
            _fail("target_ref must be a non-empty plugin-insert reference")
        if has_track and (not _is_int(params["track"]) or params["track"] < 0):
            _fail("'track' must be an Int >= 0")
        return "ref"
    if has_track:
        if not _is_int(params["track"]) or params["track"] < 0:
            _fail("'track' must be an Int >= 0")
        return "track"
    _fail("missing selector: supply 'track', 'target_ref' or strip:'output'")


def _require_insert(params: dict, selector: str) -> int | None:
    if "insert" not in params:
        if selector == "ref":
            return None
        _fail("'insert' must be an Int >= 0")
    insert = params["insert"]
    if not _is_int(insert) or insert < 0:
        _fail("'insert' must be an Int >= 0")
    return insert


def _require_write_envelope(params: dict) -> None:
    if params.get("mode") != MODE:
        _fail("mode must be %r" % MODE)
    if not _is_absolute_path(params.get("project_expected_path")):
        _fail("project_expected_path must be an absolute path")


def _check_forbidden(params: dict) -> None:
    for key in ("parameter", "param", "band", "plugin", "plugin_name", "plugin_id"):
        value = params.get(key)
        if isinstance(value, str) and value in FORBIDDEN_PARAMETER_NAMES:
            _fail("forbidden emission %r in %s" % (value, key))


def validate_call(call: dict) -> bool:
    """Raise ContractViolation unless *call* is on the verified surface."""
    if not isinstance(call, dict):
        _fail("a call must be an object")
    tool = call.get("tool")
    arguments = call.get("arguments")
    if tool not in TOOL_COMMANDS:
        _fail("unknown tool %r" % (tool,))
    if not isinstance(arguments, dict):
        _fail("arguments must be an object")
    command = arguments.get("command")
    params = arguments.get("params")
    if command not in TOOL_COMMANDS[tool]:
        _fail("command %r is not supported by %s" % (command, tool))
    if not isinstance(params, dict):
        _fail("params must be an object")
    _check_forbidden(params)

    if tool == TOOL_MIXER:
        if not _is_int(params.get("track")) or params["track"] < 0:
            _fail("mixer commands require an Int track >= 0")
        if not _is_number(params.get("value")):
            _fail("mixer commands require a numeric value")
        if "strip" in params:
            _fail("mixer commands address tracks, not the output strip")
        return True

    if tool == TOOL_PLUGINS:
        if command == "get_inventory":
            _selector(params)
            return True
        if command in PLUGIN_REF_READ_COMMANDS:
            if "strip" in params:
                _fail("%s requires a plugin-insert target_ref and rejects strip" % command)
            ref = params.get("target_ref")
            if not isinstance(ref, str) or not ref:
                _fail("%s requires a non-empty plugin-insert target_ref" % command)
            return True
        if command == "insert_verified":
            selector = _write_selector(params)
            _require_insert(params, selector)
            plugin = params.get("plugin")
            if plugin not in INSERTABLE_PLUGINS and plugin not in INSERTABLE_PLUGIN_IDS:
                _fail("plugin %r is not insertable (Gain / Channel EQ / Compressor)"
                      % (plugin,))
            if "plugin_name" in params or "plugin_id" in params:
                _fail("use the canonical 'plugin' key for insert_verified")
            expected = params.get("expected_name")
            if expected is not None and not (
                    isinstance(expected, str) and expected.strip()):
                _fail("expected_name must be a non-empty string when supplied")
            if selector == "ref" and "expected_name" in params:
                _fail("insert_verified does not accept expected_name with target_ref")
            if selector == "track" and "expected_name" not in params:
                _fail("a bare track insert requires expected_name or target_ref")
            if selector == "output" and "expected_name" not in params:
                _fail("the master strip insert requires expected_name (corroborated contract)")
            _require_write_envelope(params)
            return True
        if command == "set_eq_band_verified":
            selector = _write_selector(params)
            _require_insert(params, selector)
            band = params.get("band")
            if band not in EQ_VERIFIED_BANDS:
                _fail("band %r is not verified (Low/High Cut are observed-only)" % (band,))
            parameter = params.get("parameter")
            if parameter not in EQ_PARAMETER_NAMES:
                _fail("parameter %r is not a verified Channel EQ parameter" % (parameter,))
            if params.get("unit") != EQ_UNIT_BY_PARAMETER[parameter]:
                _fail("parameter %s requires unit %r"
                      % (parameter, EQ_UNIT_BY_PARAMETER[parameter]))
            if not _is_number(params.get("value")):
                _fail("value must be numeric")
            if "expected_name" in params:
                _fail("set_eq_band_verified does not accept expected_name")
            _require_write_envelope(params)
            return True
        if command == "set_param_verified":
            selector = _write_selector(params)
            _require_insert(params, selector)
            plugin = params.get("plugin")
            if plugin not in ("Compressor", COMPRESSOR_PLUGIN_ID):
                _fail("only the Compressor has verified-writable parameters")
            param = params.get("param")
            if param not in COMPRESSOR_VERIFIED_PARAMS:
                _fail("Compressor parameter %r is not verified" % (param,))
            if not isinstance(params.get("unit"), str) or not params["unit"]:
                _fail("set_param_verified requires a unit")
            if not _is_number(params.get("value")):
                _fail("value must be numeric")
            _require_write_envelope(params)
            return True
        _fail("unhandled plugins command %r" % (command,))

    if tool == TOOL_PROJECT:
        if not _is_absolute_path(params.get("output_root")):
            _fail("output_root must be an absolute path")
        has_projects = isinstance(params.get("projects"), list) and params["projects"]
        if not has_projects and not (isinstance(params.get("project"), str)
                                     or isinstance(params.get("path"), str)):
            _fail("export requires projects or project/path")
        if "artifacts" in params:
            kinds = params["artifacts"]
            if not isinstance(kinds, list) or not kinds:
                _fail("artifacts must be a non-empty list")
            if any(kind not in EXPORT_ARTIFACT_KINDS for kind in kinds):
                _fail("unsupported artifact kind")
            if "artifact" in params or "kind" in params:
                _fail("use either artifacts or artifact/kind, not both")
        elif "artifact" in params or "kind" in params:
            if params.get("artifact", params.get("kind")) not in EXPORT_ARTIFACT_KINDS:
                _fail("unsupported artifact kind")
        if "collision_policy" in params and params["collision_policy"] not in COLLISION_POLICIES:
            _fail("collision_policy must be fail_if_exists or skip_existing")
        if command == "export_plan":
            if "confirmed" in params:
                _fail("export_plan is a dry run and does not accept confirmed")
        else:
            if not isinstance(params.get("confirmed"), bool):
                _fail("export_run requires a boolean confirmed")
        return True

    if tool == TOOL_AUDIO:
        if command == "analyze_file":
            if not _is_absolute_path(params.get("path")):
                _fail("analyze_file requires an absolute path")
        else:
            if not _is_absolute_path(params.get("before_path")):
                _fail("compare_spectra requires an absolute before_path")
            if not _is_absolute_path(params.get("after_path")):
                _fail("compare_spectra requires an absolute after_path")
            mode = params.get("comparison_mode", "absolute")
            if mode not in COMPARISON_MODES:
                _fail("comparison_mode must be absolute or spectral_shape")
        if "output_root" in params and not _is_absolute_path(params["output_root"]):
            _fail("output_root must be an absolute path when supplied")
        return True

    _fail("unhandled tool %r" % (tool,))


def emitted_forbidden(call: dict) -> str | None:
    """Return the forbidden token in *call*, or None.  Never raises."""
    try:
        validate_call(call)
    except ContractViolation as exc:
        text = str(exc)
        for token in FORBIDDEN_EMISSIONS:
            if token in text:
                return token
        return text
    return None


def relative_source(module_path: str) -> str:
    """Provenance helper: repository-relative path for a source file."""
    return os.path.relpath(module_path, start=os.getcwd())
