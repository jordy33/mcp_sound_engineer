"""Closed loop: apply -> export -> measure -> compare -> iterate.

The loop is intentionally conservative.  It is a dry run by default; ``--apply``
is required before it creates a directory, exports audio or writes a report.
It can only correct the Channel EQ bands whose gain it wrote and whose effect it
can measure; limiter, compressor, polarity, high-pass and dither stay manual, and
the report says so instead of pretending the delivery is finished.

Exit codes:

* ``0`` converged / static-only / terminal, no manual stage pending;
* ``2`` honest stop: manual work pending, a limit was hit, or the measurement
  cannot support a correction;
* ``1`` failure: the plan was blocked, a write did not verify, an export or an
  artifact was invalid, or a guardrail broke.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sys

from server import rules

from . import mcp_client
from . import mcp_logic_contract as contract
from .cli import _parse_master, _parse_targets
from .fader_calibration import FaderCalibration
from .inventory import fetch_chain
from .logic_adapter import (
    CAPABILITIES,
    MODE,
    RecipeError,
    apply_steps,
    build_plan,
    inventory_targets,
    summary,
)

LOOP_SCHEMA = "mcp_sound_engineer.loop.v1"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
EXPORT_SCHEMA = "logic_pro_mcp_export_run.v1"
DEFAULT_ENDPOINT = "http://127.0.0.1:8765/mcp"

BLOCKING_STATUSES = frozenset({"unresolved", "needs_inventory", "needs_calibration"})
FAILURE_REASONS = frozenset({
    "trusted_before_required", "plan_blocked", "output_root_escape",
    "export_failed", "state_not_verified", "dependency_skipped",
    "artifact_mismatch", "artifact_changed", "artifact_unreadable",
    "bounce_settings_unverified", "bounce_settings_changed",
    "analysis_failed", "guardrail_failed", "recipe_invalid",
})
HONEST_REASONS = frozenset({
    "manual_only", "manual_delivery", "partial_manual", "max_iterations",
    "no_improvement", "correction_cap", "sign_mismatch", "effect_mismatch",
    "residual_unavailable", "residual_low_trust", "master_eq_advisory",
    "band_enablement_unverified",
})
SUCCESS_REASONS = frozenset({"tolerance", "static_only", "readback_confirmed",
                             "recipe_terminal", "dry_run"})
DYNAMIC_MANUAL_ACTIONS = frozenset({"limiter", "compressor"})
LOW_TRUST_DISPOSITIONS = frozenset({"residual_low_trust", "sign_mismatch",
                                    "effect_mismatch", "residual_unavailable"})


class _Stop(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


class LoopConfig:
    def __init__(self, *, project: str, recipe: list, output_root: str,
                 tracks: dict | None = None, master: dict | None = None,
                 endpoint: str = DEFAULT_ENDPOINT, run_id: str | None = None,
                 before: str | None = None, trust_before: bool = False,
                 fader_calibration=None, overwrite_eq: bool = False,
                 max_iterations: int = 2, tolerance_db: float = 1.0,
                 incremental_tolerance_db: float = 0.5,
                 max_correction_db: float = 1.0,
                 max_cumulative_correction_db: float = 3.0,
                 no_improvement_db: float = 0.2,
                 allow_unverified_bounce_settings: bool = False,
                 apply: bool = False, report_path: str | None = None):
        self.project = project
        self.recipe = recipe
        self.output_root = output_root
        self.tracks = tracks or {}
        self.master = master
        self.endpoint = endpoint
        self.run_id = run_id
        self.before = before
        self.trust_before = trust_before
        self.fader_calibration = fader_calibration
        self.overwrite_eq = overwrite_eq
        self.max_iterations = max_iterations
        self.tolerance_db = tolerance_db
        self.incremental_tolerance_db = incremental_tolerance_db
        self.max_correction_db = max_correction_db
        self.max_cumulative_correction_db = max_cumulative_correction_db
        self.no_improvement_db = no_improvement_db
        self.allow_unverified_bounce_settings = allow_unverified_bounce_settings
        self.apply = apply
        self.report_path = report_path


# ---------------------------------------------------------------------------
# file helpers
# ---------------------------------------------------------------------------
def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_fingerprint(path: str) -> str:
    return "sha256:%s" % sha256_file(path)


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _number(value):
    return float(value) if _is_number(value) else None


def _contained(path: str, root: str) -> bool:
    real_path = os.path.realpath(path)
    real_root = os.path.realpath(root)
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def _symlink_within(path: str, root: str) -> bool:
    """Reject a symlink at or below *root* on the way to *path*."""
    current = os.path.abspath(path)
    root = os.path.abspath(root)
    while True:
        if os.path.islink(current):
            return True
        if current == root:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _canonical_recipe_sha(recipe) -> str:
    text = json.dumps(recipe, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _silence_ratio(path: str) -> float | None:
    try:
        import numpy as np
        from server import analysis
        data, sr = analysis.load(path)
    except Exception:  # noqa: BLE001 - reported as an unreadable baseline
        return None
    if data.size == 0 or sr <= 0:
        return None
    mono = data.mean(axis=1) if data.ndim > 1 else data
    window = max(1, int(0.1 * sr))
    frames = len(mono) // window
    if frames == 0:
        return 0.0 if float(np.max(np.abs(mono))) > 0 else 1.0
    rms = np.sqrt(np.mean(mono[:frames * window].reshape(frames, window) ** 2,
                          axis=1))
    threshold = 10 ** (-60.0 / 20.0)
    return round(float(np.mean(rms < threshold)), 4)


def default_measure(path: str) -> dict:
    from server import analysis
    metrics = analysis.analyze_master(path)
    metrics["silence_ratio"] = _silence_ratio(path)
    return metrics


# ---------------------------------------------------------------------------
# response extraction
# ---------------------------------------------------------------------------
def _payload(result, reason: str) -> dict:
    if not getattr(result, "ok", False):
        raise _Stop(reason, getattr(result, "error", None) or "transport failure")
    payload = getattr(result, "payload", None)
    if not isinstance(payload, dict) or not payload:
        raise _Stop(reason, "the bridge returned no structured payload")
    return payload


def _artifact_list(payload: dict) -> list:
    for key in ("artifacts", "expected_artifacts", "expectedArtifacts"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _artifact_path(payload: dict) -> str | None:
    artifacts = _artifact_list(payload)
    if not artifacts or not isinstance(artifacts[0], dict):
        return None
    artifact = artifacts[0]
    evidence = artifact.get("evidence") if isinstance(artifact.get("evidence"), dict) else {}
    for value in (artifact.get("path"), artifact.get("output_path"),
                  evidence.get("path")):
        if isinstance(value, str) and value:
            return value
    return None


def _plan_issues(payload: dict) -> list:
    issues = []
    if payload.get("status") != "planned":
        issues.append("status=%r (expected planned)" % (payload.get("status"),))
    projects = payload.get("projects")
    if not isinstance(projects, list) or len(projects) != 1:
        issues.append("expected exactly one project")
    else:
        project = projects[0] if isinstance(projects[0], dict) else {}
        if project.get("validation_status") != "valid":
            issues.append("project validation_status=%r"
                          % (project.get("validation_status"),))
        if project.get("validation_issues") not in ([], None):
            issues.append("project validation_issues is not empty")
    artifacts = _artifact_list(payload)
    if len(artifacts) != 1:
        issues.append("expected exactly one artifact")
    elif not isinstance(artifacts[0], dict):
        issues.append("artifact is not an object")
    else:
        artifact = artifacts[0]
        if artifact.get("kind") != "bounce":
            issues.append("artifact kind=%r (expected bounce)"
                          % (artifact.get("kind"),))
        if artifact.get("status") != "pending":
            issues.append("artifact status=%r (expected pending)"
                          % (artifact.get("status"),))
        verification = artifact.get("verification")
        if not isinstance(verification, dict):
            issues.append("artifact verification is missing")
        else:
            if verification.get("issues") not in ([], None):
                issues.append("artifact verification.issues is not empty")
            if verification.get("path_under_output_root") is not True:
                issues.append("artifact path is not under output_root")
            if verification.get("would_overwrite") is not False:
                issues.append("artifact would overwrite an existing file")
    for step in payload.get("unsupported_or_blocked_steps") or []:
        operation = step.get("operation") if isinstance(step, dict) else step
        if operation != "cloud_delivery":
            issues.append("unsupported export step: %r" % (operation,))
    return issues


def _run_issues(payload: dict) -> list:
    issues = []
    counters = payload.get("counters") if isinstance(payload.get("counters"), dict) else payload
    if payload.get("schema") != EXPORT_SCHEMA:
        issues.append("schema=%r" % (payload.get("schema"),))
    if payload.get("status") != "completed":
        issues.append("status=%r (expected completed)" % (payload.get("status"),))
    if payload.get("confirmed") is not True:
        issues.append("confirmed is not true")
    if payload.get("collision_policy") != "fail_if_exists":
        issues.append("collision_policy=%r" % (payload.get("collision_policy"),))
    if counters.get("project_count") != 1:
        issues.append("project_count=%r" % (counters.get("project_count"),))
    if counters.get("artifacts_total") != 1:
        issues.append("artifacts_total=%r" % (counters.get("artifacts_total"),))
    if counters.get("artifacts_verified") != 1:
        issues.append("artifacts_verified=%r" % (counters.get("artifacts_verified"),))
    for key in ("artifacts_skipped", "artifacts_uncertain", "artifacts_failed"):
        if counters.get(key) != 0:
            issues.append("%s=%r" % (key, counters.get(key)))
    projects = payload.get("projects")
    if not isinstance(projects, list) or len(projects) != 1:
        issues.append("expected exactly one exported project")
    else:
        project = projects[0] if isinstance(projects[0], dict) else {}
        if project.get("identity_verified") is not True:
            issues.append("project identity was not verified")
        if project.get("opened") is not True:
            issues.append("project was not opened")
    artifacts = _artifact_list(payload)
    if len(artifacts) != 1 or not isinstance(artifacts[0], dict):
        issues.append("expected exactly one artifact")
        return issues
    artifact = artifacts[0]
    if artifact.get("kind") != "bounce":
        issues.append("artifact kind=%r (expected bounce)" % (artifact.get("kind"),))
    if artifact.get("state") != "A":
        issues.append("artifact state=%r (expected A)" % (artifact.get("state"),))
    if artifact.get("verified") is not True:
        issues.append("artifact was not verified")
    if artifact.get("bounce_fired") is not True:
        issues.append("bounce did not fire")
    if artifact.get("write_attempted") is not True:
        issues.append("bounce was not attempted")
    if artifact.get("error") is not None:
        issues.append("artifact error=%r" % (artifact.get("error"),))
    if artifact.get("reason") is not None:
        issues.append("artifact reason=%r" % (artifact.get("reason"),))
    evidence = artifact.get("evidence")
    if not isinstance(evidence, dict):
        issues.append("artifact evidence is missing")
    else:
        if evidence.get("exists") is not True:
            issues.append("artifact does not exist")
        for key in ("file_size_bytes", "duration_seconds", "sample_rate",
                    "channel_count"):
            value = evidence.get(key)
            if not _is_number(value) or float(value) <= 0:
                issues.append("evidence.%s=%r" % (key, value))
    path = _artifact_path(payload)
    if not isinstance(path, str) or not os.path.isabs(path):
        issues.append("artifact path is missing or not absolute")
    return issues


def _settings_from(payload: dict) -> tuple:
    artifact = (_artifact_list(payload) or [{}])[0]
    if not isinstance(artifact, dict):
        artifact = {}
    status = artifact.get("bounce_settings_read_status")
    if status is None:
        status = payload.get("bounce_settings_read_status")
    settings = artifact.get("bounce_settings")
    if settings is None:
        settings = payload.get("bounce_settings")
    return status, settings


# ---------------------------------------------------------------------------
# residual comparison helpers
# ---------------------------------------------------------------------------
def _fingerprint(payload: dict, side: str):
    value = payload.get(side)
    if isinstance(value, dict):
        for key in ("artifactFingerprint", "fingerprint", "sha256"):
            if isinstance(value.get(key), str):
                return value[key]
    for key in ("%sFingerprint" % side, "%s_fingerprint" % side,
                "%sArtifactFingerprint" % side):
        if isinstance(payload.get(key), str):
            return payload[key]
    return None


def _residual_bands(payload: dict) -> list:
    shape = payload.get("shapeComparison")
    if isinstance(shape, dict):
        for key in ("bands", "residualBands", "residual_db"):
            value = shape.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [{"centerHz": center, "residualDb": residual}
                        for center, residual in value.items()]
    for key in ("bands", "residualBands"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _center_hz(item: dict):
    for key in ("centerHz", "center_hz", "frequency_hz", "frequency", "center"):
        if _is_number(item.get(key)):
            return float(item[key])
    return None


def _residual_value(item: dict):
    for key in ("residualDb", "residual_db", "residual"):
        if key in item:
            value = item[key]
            if _is_number(value):
                return float(value), None
            reason = item.get("unavailableReason") or item.get("unavailable_reason")
            return None, reason or "residual_unavailable"
    return None, (item.get("unavailableReason")
                  or item.get("unavailable_reason")
                  or "residual_unavailable")


def _window_effect(bands: list, freq_hz: float, q: float) -> dict:
    half = 0.72 / q if q and q > 0 else 0.72
    points = []
    for item in bands:
        if not isinstance(item, dict):
            continue
        center = _center_hz(item)
        if center is None or center <= 0:
            continue
        distance = abs(math.log2(center / freq_hz))
        points.append((distance, center, item))
    if not points:
        return {"effect_db": None, "reason": "residual_unavailable",
                "half_width_octaves": round(half, 3), "bands": []}
    inside = [point for point in points if point[0] <= half]
    if not inside:
        nearest = min(points, key=lambda point: point[0])
        inside = [nearest]
    values = []
    detail = []
    ordered = sorted(inside, key=lambda point: point[0])
    for distance, center, item in ordered:
        value, reason = _residual_value(item)
        if value is None:
            return {"effect_db": None, "reason": reason or "residual_unavailable",
                    "half_width_octaves": round(half, 3),
                    "bands": [entry[1] for entry in ordered]}
        values.append(value)
        detail.append({"center_hz": center, "distance_octaves": round(distance, 4),
                       "residual_db": value})
    return {"effect_db": sum(values) / len(values), "reason": None,
            "half_width_octaves": round(half, 3), "bands": detail}


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
class LoopRunner:
    def __init__(self, config: LoopConfig, *, bridge=None, connect=None,
                 measure=None, metadata_reader=None, planner=None,
                 applier=None, hash_file=None, copy_file=None,
                 now=None, make_dir=None):
        self.config = config
        self._bridge = bridge
        self._connect = connect
        self.measure = measure or default_measure
        self.metadata_reader = metadata_reader
        self.planner = planner or build_plan
        self.applier = applier or apply_steps
        self.hash_file = hash_file or sha256_file
        self.copy_file = copy_file or shutil.copyfile
        self.now = now or datetime.datetime.now
        self.make_dir = make_dir or os.makedirs
        self.owns_bridge = False
        self.bridge_error = None

        self.run_id = None
        self.output_root = None
        self.run_root = None
        self.steps: list = []
        self.inventory_error = None
        self.manual_steps: list = []
        self.terminal_steps: list = []
        self.blocked_steps: list = []
        self.dynamic_targets: set = set()
        self.ceiling_dbtp = None
        self.master_eq_written = False
        self.manual_delivery = False
        self.bindings: dict = {}
        self.operations: list = []
        self.warnings: list = []
        self.baseline = None
        self.stage = None
        self.exported_runs: list = []
        self.iterations: list = []
        self.base_run = None
        self.bell_records: list = []
        self.stop_reason = None
        self.stop_detail = None
        self.report_written = None

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> dict:
        try:
            self._execute()
            raise _Stop("analysis_failed", "the loop ended without a reason")
        except _Stop as stop:
            self.stop_reason = stop.reason
            self.stop_detail = stop.detail
        except Exception as exc:  # noqa: BLE001 - report, never crash the CLI
            self.stop_reason = "analysis_failed"
            self.stop_detail = "internal error: %s: %s" % (
                type(exc).__name__, exc)
        finally:
            self._close_bridge()
        report = self._report()
        if self.config.apply and self.config.report_path:
            self._write_report(report)
        return report

    def _stop(self, reason: str, detail: str | None = None):
        raise _Stop(reason, detail)

    def _ensure_bridge(self, required: bool) -> bool:
        if self._bridge is not None:
            return True
        if self.bridge_error is not None:
            if required:
                self._stop("plan_blocked",
                           "cannot reach the bridge: %s" % self.bridge_error)
            return False
        connect = self._connect or (lambda endpoint: mcp_client.connect(endpoint))
        try:
            self._bridge = connect(self.config.endpoint)
            self.owns_bridge = True
            return True
        except Exception as exc:  # noqa: BLE001
            self.bridge_error = exc
            message = "cannot reach the bridge: %s" % exc
            if required:
                self._stop("plan_blocked", message)
            self.warnings.append(message)
            return False

    def _close_bridge(self) -> None:
        if self.owns_bridge and self._bridge is not None:
            try:
                self._bridge.close()
            except Exception:  # noqa: BLE001
                pass
            self._bridge = None

    def _bridge_call(self, tool: str, command: str, params: dict,
                     timeout: float, reason: str):
        self._ensure_bridge(True)
        arguments = {"command": command, "params": params}
        try:
            result = self._bridge.call_tool(tool, arguments, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self._stop(reason, "%s failed: %s" % (command, exc))
        if not getattr(result, "ok", False):
            self._stop(reason, "%s failed: %s" % (
                command, getattr(result, "error", None) or "transport failure"))
        return result

    # -- preflight ---------------------------------------------------------
    def _execute(self) -> None:
        self._validate_paths()
        self.steps = self._plan(None)
        self._classify()

        targets = inventory_targets(self.config.recipe, self.config.tracks,
                                    self.config.master)
        if targets:
            chains = self._fetch_inventories(targets)
            self.steps = self._plan(chains)
            self._classify()

        self._preflight()

        if not self.config.apply:
            self._stop("dry_run")

        self._ensure_bridge(True)
        self._create_run_root()
        if self.config.before:
            self.baseline = self._external_baseline()
        else:
            self.baseline = self._exported_baseline()
        self.base_run = self.baseline
        self._guard_measure(self.baseline, baseline=True)
        self._flag_manual_delivery(self.baseline)

        static = [step for step in self.steps
                  if step.get("phase") == "static" and step.get("calls")]
        bells = [step for step in self.steps
                 if step.get("phase") == "bell" and step.get("calls")]
        if static:
            self._apply_phase(static, "static")
            self.stage = self._exported_run("stage")
            self._guard_measure(self.stage)
            self._flag_manual_delivery(self.stage)
            self.base_run = self.stage
        if not bells:
            if self.stage is not None:
                self._finish("static_only")
            self._stop("manual_only")
        self._bells(bells)

    def _validate_paths(self) -> None:
        if self.config.before and not self.config.trust_before:
            self._stop("trusted_before_required",
                       "--before is external evidence; pass --trust-before "
                       "to accept it as the baseline")
        if not self.config.project or not os.path.isabs(self.config.project):
            self._stop("plan_blocked", "--project must be an absolute .logicx path")
        output_root = self.config.output_root
        if not output_root or not os.path.isdir(output_root):
            self._stop("output_root_escape",
                       "--output-root must be an existing directory")
        self.output_root = os.path.realpath(output_root)
        run_id = self.config.run_id or self.now().strftime("%Y-%m-%dT%H-%M-%S")
        if run_id in (".", "..") or not RUN_ID_RE.match(run_id):
            self._stop("output_root_escape",
                       "run id %r must match %s" % (run_id, RUN_ID_RE.pattern))
        self.run_id = run_id
        self.run_root = os.path.join(self.output_root, run_id)
        if not _contained(self.run_root, self.output_root):
            self._stop("output_root_escape", "run root escapes --output-root")
        if self.config.max_iterations < 1:
            self._stop("plan_blocked", "--max-iterations must be >= 1")
        for name in ("tolerance_db", "incremental_tolerance_db",
                     "max_correction_db", "max_cumulative_correction_db",
                     "no_improvement_db"):
            value = getattr(self.config, name)
            if not _is_number(value) or value < 0:
                self._stop("plan_blocked", "--%s must be a non-negative number"
                           % name.replace("_", "-"))

    def _plan(self, inventory) -> list:
        try:
            return self.planner(
                self.config.recipe, self.config.tracks, self.config.master,
                self.config.project, self.config.fader_calibration,
                inventory=inventory, overwrite_eq=self.config.overwrite_eq)
        except RecipeError as exc:
            self._stop("recipe_invalid", str(exc))

    def _fetch_inventories(self, targets) -> dict:
        if not self._ensure_bridge(required=self.config.apply):
            return {}
        chains = {}
        for entry in targets:
            target = entry["target"]
            selector = (dict(contract.MASTER_SELECTOR) if target.get("is_master")
                        else {"track": target["track"]})
            key = "master" if target.get("is_master") else "track:%d" % target["track"]
            chain = fetch_chain(self._bridge, selector, key=key)
            chains[key] = chain
            if not chain.reliable:
                detail = ", ".join(chain.issues) or chain.state or "unavailable"
                if self.config.apply:
                    self.warnings.append(
                        "inventory for %s is not reliable: %s" % (key, detail))
        return chains

    def _classify(self) -> None:
        self.manual_steps = [step for step in self.steps
                             if step.get("status") == "manual"]
        self.terminal_steps = [step for step in self.steps
                               if step.get("status") == "terminal"]
        self.blocked_steps = [step for step in self.steps
                              if step.get("status") in BLOCKING_STATUSES]
        self.dynamic_targets = {
            step.get("target_key") for step in self.manual_steps
            if step.get("action") in DYNAMIC_MANUAL_ACTIONS
        }
        for step in self.steps:
            self.warnings.extend(step.get("warnings") or [])
        ceilings = [
            float(action["ceiling_dbtp"])
            for action in self.config.recipe
            if isinstance(action, dict) and action.get("action") == "limiter"
            and _is_number(action.get("ceiling_dbtp"))
        ]
        self.ceiling_dbtp = min(ceilings) if ceilings else None

    def _preflight(self) -> None:
        if self.terminal_steps:
            self._stop("recipe_terminal", "send_back terminates the recipe")
        if self.config.apply and self.blocked_steps:
            detail = "; ".join(
                "%s on %s: %s" % (step.get("action"),
                                  step.get("target_key") or step.get("target"),
                                  step.get("reason"))
                for step in self.blocked_steps)
            self._stop("plan_blocked", detail)
        if not self.blocked_steps and not any(
                step.get("calls") for step in self.steps
                if step.get("status") == "ready"):
            self._stop("manual_only", "no automatable action remains")

    # -- plan shapes -------------------------------------------------------
    def _plan_payload(self) -> dict:
        report = {
            "summary": summary(self.steps),
            "steps": self.steps,
            "manual": [{"action": step.get("action"),
                        "target": step.get("target"),
                        "reason": step.get("reason"),
                        "hint": step.get("hint")}
                       for step in self.manual_steps],
            "blocked": [{"action": step.get("action"),
                         "target": step.get("target"),
                         "status": step.get("status"),
                         "reason": step.get("reason")}
                        for step in self.blocked_steps],
        }
        return report

    # -- exports -----------------------------------------------------------
    def _make_directory(self, path: str) -> None:
        if not _contained(path, self.run_root) or _symlink_within(path, self.run_root):
            self._stop("output_root_escape",
                       "refusing to write outside the run root: %s" % path)
        if os.path.exists(path):
            self._stop("output_root_escape",
                       "iteration directory already exists: %s" % path)
        try:
            self.make_dir(path, exist_ok=False)
        except FileExistsError:
            self._stop("output_root_escape",
                       "iteration directory already exists: %s" % path)
        except OSError as exc:
            self._stop("export_failed", "cannot create %s: %s" % (path, exc))

    def _export_params(self, output_root: str, confirmed: bool = False) -> dict:
        params = {
            "output_root": output_root,
            "projects": [self.config.project],
            "collision_policy": "fail_if_exists",
            "artifact": "bounce",
        }
        if confirmed:
            params["confirmed"] = True
        return params

    def _exported_run(self, label: str) -> dict:
        self._ensure_bridge(True)
        directory = os.path.join(self.run_root, label)
        self._make_directory(directory)
        plan_params = self._export_params(directory)
        plan_result = self._bridge_call(
            contract.TOOL_PROJECT, "export_plan", plan_params,
            mcp_client.EXPORT_TIMEOUT, "export_failed")
        plan_payload = _payload(plan_result, "export_failed")
        plan_issues = _plan_issues(plan_payload)
        if plan_issues:
            self._stop("export_failed",
                       "export_plan [%s]: %s" % (label, "; ".join(plan_issues)))
        run_params = dict(plan_params, confirmed=True)
        run_result = self._bridge_call(
            contract.TOOL_PROJECT, "export_run", run_params,
            mcp_client.EXPORT_TIMEOUT, "export_failed")
        run_payload = _payload(run_result, "export_failed")
        run_issues = _run_issues(run_payload)
        if run_issues:
            self._stop("export_failed",
                       "export_run [%s]: %s" % (label, "; ".join(run_issues)))
        path = _artifact_path(run_payload)
        if not _contained(path, directory) or _symlink_within(path, directory):
            self._stop("output_root_escape",
                       "exported artifact is outside %s: %s" % (directory, path))
        if not os.path.isfile(path):
            self._stop("export_failed", "exported artifact is missing: %s" % path)
        digest = self.hash_file(path)
        status, settings = _settings_from(run_payload)
        artifacts = _artifact_list(run_payload)
        evidence = {}
        if artifacts and isinstance(artifacts[0], dict) \
                and isinstance(artifacts[0].get("evidence"), dict):
            evidence = artifacts[0]["evidence"]
        record = {
            "label": label,
            "dir": directory,
            "path": path,
            "sha256": digest,
            "metrics": None,
            "guards": None,
            "audio_metadata": None,
            "bounce_settings": settings,
            "bounce_settings_read_status": status,
            "evidence": evidence if isinstance(evidence, dict) else {},
            "export_plan": {
                "run_id": plan_payload.get("plan_id")
                or plan_payload.get("run_id") or plan_payload.get("export_plan_id"),
                "status": plan_payload.get("status"),
            },
            "export_run": {
                "run_id": run_payload.get("run_id"),
                "status": run_payload.get("status"),
                "counters": run_payload.get("counters") or {
                    key: run_payload.get(key) for key in (
                        "project_count", "artifacts_total", "artifacts_verified",
                        "artifacts_skipped", "artifacts_uncertain",
                        "artifacts_failed")},
            },
        }
        # Keep every bounce the loop produced, including one whose guard
        # rejects it: the report must not hide an artifact that exists.
        self.exported_runs.append(record)
        self._check_settings(record)
        self._read_metrics(record)
        if self.metadata_reader is not None:
            try:
                record["audio_metadata"] = self.metadata_reader(path)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append("analyze_file [%s] failed: %s" % (label, exc))
        return record

    def _check_settings(self, record: dict) -> None:
        status = record.get("bounce_settings_read_status")
        settings = record.get("bounce_settings")
        if status != "ok" or not isinstance(settings, dict):
            if self.config.allow_unverified_bounce_settings:
                self.warnings.append(
                    "bounce settings unverified for %s (explicitly allowed)"
                    % record["label"])
            else:
                self._stop("bounce_settings_unverified",
                           "bounce settings are not readable for %s"
                           % record["label"])
        reference = (self.baseline or {}).get("bounce_settings")
        if reference is not None and isinstance(settings, dict) \
                and settings != reference:
            self._stop("bounce_settings_changed",
                       "bounce settings changed in %s" % record["label"])

    def _read_metrics(self, record: dict) -> None:
        try:
            record["metrics"] = self.measure(record["path"])
        except Exception as exc:  # noqa: BLE001
            self._stop("artifact_unreadable",
                       "cannot analyze %s: %s" % (record["path"], exc))
        if not isinstance(record["metrics"], dict):
            self._stop("artifact_unreadable",
                       "analysis for %s is not an object" % record["path"])

    def _external_baseline(self) -> dict:
        source = self.config.before
        if not os.path.isabs(source):
            self._stop("artifact_unreadable", "--before must be absolute")
        if not os.path.isfile(source):
            self._stop("artifact_unreadable", "baseline not found: %s" % source)
        baseline_dir = os.path.join(self.run_root, "baseline")
        self._make_directory(baseline_dir)
        copy_path = os.path.join(baseline_dir,
                                 "external-%s" % os.path.basename(source))
        try:
            self.copy_file(source, copy_path)
        except OSError as exc:
            self._stop("artifact_unreadable", "cannot copy --before: %s" % exc)
        digest = self.hash_file(copy_path)
        record = {
            "label": "baseline",
            "source": "trusted_external",
            "origin_path": os.path.abspath(source),
            "path": copy_path,
            "dir": baseline_dir,
            "sha256": digest,
            "metrics": None,
            "guards": None,
            "audio_metadata": None,
            "bounce_settings": None,
            "bounce_settings_read_status": None,
            "evidence": {},
            "export_plan": None,
            "export_run": None,
        }
        self.warnings.append("baseline_provenance_unverified")
        self.warnings.append(
            "baseline_copy_from_external: the copy is not a fresh bounce of "
            "this project")
        self._read_metrics(record)
        return record

    def _exported_baseline(self) -> dict:
        record = self._exported_run("baseline")
        record["source"] = "exported"
        return record

    def _create_run_root(self) -> None:
        if not _contained(self.run_root, self.output_root):
            self._stop("output_root_escape", "run root escapes --output-root")
        try:
            self.make_dir(self.run_root, exist_ok=True)
        except OSError as exc:
            self._stop("output_root_escape",
                       "cannot create the run root: %s" % exc)
        if _symlink_within(self.run_root, self.output_root):
            self._stop("output_root_escape",
                       "the run root contains a symlink component")

    # -- metrics and guards ------------------------------------------------
    @staticmethod
    def _loudness(metrics: dict) -> dict:
        value = metrics.get("loudness") if isinstance(metrics, dict) else None
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _duration(metrics: dict):
        return _number(metrics.get("duration_seconds"))

    @staticmethod
    def _sample_peak(metrics: dict):
        return _number(LoopRunner._loudness(metrics).get("sample_peak_dbfs"))

    @staticmethod
    def _true_peak(metrics: dict):
        return _number(LoopRunner._loudness(metrics).get("true_peak_dbtp"))

    def _guard_measure(self, record: dict, baseline: bool = False) -> None:
        metrics = record["metrics"]
        duration = self._duration(metrics)
        loud = _number(self._loudness(metrics).get("integrated_lufs"))
        silence = _number(metrics.get("silence_ratio"))
        peak = self._sample_peak(metrics)
        if duration is None or duration <= 0:
            self._stop("artifact_unreadable", "artifact has no duration")
        if baseline:
            if loud is None or loud <= -70.0:
                self._stop("artifact_unreadable",
                           "baseline is silent or unmeasurable (%.2f LUFS)"
                           % (loud if loud is not None else float("nan")))
            if silence is None or silence >= 0.95:
                self._stop("artifact_unreadable",
                           "baseline is mostly silence (silence_ratio=%r)"
                           % (silence,))
            if peak is None or peak <= -60.0:
                self._stop("artifact_unreadable",
                           "baseline has no measurable peak")
            record["guards"] = {"baseline_valid": True}
            return

        base = self.baseline["metrics"]
        issues = []
        metadata_issues = []
        base_sr = base.get("sample_rate")
        base_ch = base.get("channels")
        base_duration = self._duration(base)
        if metrics.get("sample_rate") != base_sr:
            metadata_issues.append("sample rate changed")
        if metrics.get("channels") != base_ch:
            metadata_issues.append("channel count changed")
        if base_duration is not None:
            tolerance = max(0.25, 0.005 * base_duration)
            if abs(duration - base_duration) > tolerance:
                metadata_issues.append("duration changed")
        if silence is None or silence >= 0.95:
            issues.append("silence_ratio >= 0.95")
        if peak is None or peak <= -60.0:
            issues.append("sample peak <= -60 dBFS")
        current_clip = (metrics.get("clipping") or {}).get("count")
        base_clip = (self.baseline["metrics"].get("clipping") or {}).get("count")
        if _is_number(current_clip) and _is_number(base_clip) \
                and current_clip > base_clip:
            issues.append("clipping increased")
        phase = _number((metrics.get("stereo") or {}).get("phase_correlation"))
        base_phase = _number((base.get("stereo") or {}).get("phase_correlation"))
        if phase is None or phase < 0.3:
            issues.append("phase correlation below 0.3")
        elif base_phase is not None and phase < base_phase - 0.15:
            issues.append("phase correlation dropped more than 0.15")
        sibilance = _number((metrics.get("spectral") or {}).get("sibilance_db_rel"))
        base_sibilance = _number(
            (base.get("spectral") or {}).get("sibilance_db_rel"))
        if sibilance is not None and base_sibilance is not None \
                and sibilance > base_sibilance + 1.0:
            issues.append("sibilance worsened by more than 1 dB")
        record["guards"] = {
            "metadata": metadata_issues,
            "guardrails": issues,
            "silence_ratio": silence,
            "clipping": current_clip,
            "phase_correlation": phase,
            "sibilance_db_rel": sibilance,
        }
        if metadata_issues:
            self._stop("artifact_mismatch", "; ".join(metadata_issues))
        if issues:
            self._stop("guardrail_failed", "; ".join(issues))

    def _flag_manual_delivery(self, record: dict) -> None:
        if self.ceiling_dbtp is None:
            return
        true_peak = self._true_peak(record["metrics"])
        if true_peak is not None and true_peak > self.ceiling_dbtp + 0.1:
            self.manual_delivery = True
            self.warnings.append(
                "true peak %.2f dBTP is above the %.2f dBTP ceiling; the "
                "limiter is manual and was not applied"
                % (true_peak, self.ceiling_dbtp))

    # -- apply -------------------------------------------------------------
    def _apply_phase(self, steps: list, phase: str) -> list:
        self._ensure_bridge(True)
        try:
            results = self.applier(
                steps, client=self._bridge, phases={phase},
                bindings=self.bindings)
        except Exception as exc:  # noqa: BLE001
            self._stop("state_not_verified", "apply failed: %s" % exc)
        for result in results:
            result = dict(result)
            result["phase"] = phase
            self.operations.append(result)
        for step in steps:
            if step.get("target_selector", {}).get("strip") == contract.MASTER_STRIP:
                if phase in ("static", "bell"):
                    self.master_eq_written = True
        bad = [result for result in results
               if result.get("status") in ("error", "partial",
                                           "skipped_dependency")]
        if bad:
            partial = any(result.get("status") == "partial" for result in bad)
            dependency = any(result.get("reason_code") == "dependency_skipped"
                             for result in bad)
            enablement = any(
                result.get("reason_code") == "band_enablement_unverified"
                for result in bad)
            if enablement and not partial:
                reason = "band_enablement_unverified"
            elif dependency and not partial:
                reason = "dependency_skipped"
            else:
                reason = "state_not_verified"
            detail = "; ".join(
                "%s: %s" % (result.get("operation") or result.get("step"),
                            result.get("detail") or result.get("error")
                            or result.get("reason"))
                for result in bad[:4])
            self._stop(reason, detail)
        return results

    # -- bells -------------------------------------------------------------
    def _bell_records(self) -> list:
        records = []
        for index, step in enumerate(self.steps):
            if step.get("phase") != "bell" or step.get("status") != "ready":
                continue
            action = (self.config.recipe[index]
                      if index < len(self.config.recipe) else {})
            if not isinstance(action, dict) or action.get("action") != "eq.bell":
                continue
            selector = step.get("target_selector") or {}
            target_key = step.get("target_key")
            binding = self.bindings.get(target_key) or {}
            ref = binding.get("ref")
            if not ref:
                for call in step.get("calls") or []:
                    ref = call["arguments"]["params"].get("target_ref")
                    if ref:
                        break
            gain = _number(action.get("gain_db"))
            freq = _number(action.get("freq_hz"))
            q = _number(action.get("q"))
            if gain is None or freq is None or freq <= 0:
                self._stop("plan_blocked",
                           "bell step %s lacks a numeric gain/frequency" % index)
            records.append({
                "step": index,
                "action": action.get("action"),
                "target_key": target_key,
                "target_selector": selector,
                "is_master": selector.get("strip") == contract.MASTER_STRIP,
                "track": selector.get("track"),
                "band": step.get("band"),
                "insert": (binding.get("insert")
                           if binding.get("insert") is not None
                           else step.get("insert")),
                "ref": ref,
                "freq_hz": freq,
                "q": q if q and q > 0 else 1.0,
                "gain_recipe": gain,
                "gain_prev": 0.0,
                "gain": gain,
                "readback": None,
                "corrections": [],
            })
        return records

    def _readback(self, records: list) -> None:
        by_target = {}
        for record in records:
            if record["is_master"]:
                continue
            by_target.setdefault(record["target_key"], []).append(record)
        for target_key, items in by_target.items():
            ref = items[0].get("ref")
            if not ref:
                self._stop("state_not_verified",
                           "track Channel EQ has no plugin_insert_ref; "
                           "cannot read the gain back")
            call = {
                "tool": contract.TOOL_PLUGINS,
                "arguments": {"command": "get_channel_eq_state_verified",
                              "params": {"target_ref": ref}},
            }
            try:
                contract.validate_call(call)
            except contract.ContractViolation as exc:
                self._stop("state_not_verified", str(exc))
            result = self._bridge_call(
                contract.TOOL_PLUGINS, "get_channel_eq_state_verified",
                {"target_ref": ref}, mcp_client.WRITE_TIMEOUT,
                "state_not_verified")
            if not getattr(result, "applied", False):
                self._stop("state_not_verified",
                           "channel EQ readback did not reach State A: %s"
                           % result.describe())
            payload = result.payload if isinstance(result.payload, dict) else {}
            if payload.get("complete") is not True:
                self._stop("state_not_verified",
                           "channel EQ readback is incomplete")
            observed = {}
            for item in payload.get("bands") or []:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    observed[item["name"]] = item
            for record in items:
                item = observed.get(record["band"])
                gain = (item or {}).get("gain")
                enabled = (item or {}).get("enabled")
                if not isinstance(gain, dict) or gain.get("read_status") != "read" \
                        or not _is_number(gain.get("observed_raw")):
                    self._stop("state_not_verified",
                               "band %r gain is unreadable" % record["band"])
                if not isinstance(enabled, dict) \
                        or enabled.get("read_status") != "read" \
                        or enabled.get("observed_raw") is not True:
                    self._stop("band_enablement_unverified",
                               "band %r is not verifiably enabled"
                               % record["band"])
                observed_gain = float(gain["observed_raw"])
                if abs(observed_gain - record["gain"]) > 0.1:
                    self._stop("state_not_verified",
                               "band %r readback %.2f dB != requested %.2f dB"
                               % (record["band"], observed_gain, record["gain"]))
                record["readback"] = {
                    "requested_db": record["gain"],
                    "observed_db": observed_gain,
                    "read_status": gain.get("read_status"),
                    "ok": True,
                }

    def _bells(self, bell_steps: list) -> None:
        self._apply_phase(bell_steps, "bell")
        records = self._bell_records()
        self.bell_records = records
        if not records:
            self._stop("plan_blocked", "no bell could be planned")
        self._readback(records)
        self._check_overlap(records)

        prev_run = self.base_run
        previous_mean = None
        for iteration in range(1, self.config.max_iterations + 1):
            label = "iter-%d" % iteration
            run = self._exported_run(label)
            self._guard_measure(run)
            self._flag_manual_delivery(run)
            incremental = self._compare_runs(prev_run, run)
            if prev_run["path"] == self.base_run["path"]:
                overall = incremental
            else:
                overall = self._compare_runs(self.base_run, run)
            entry = {
                "iteration": iteration,
                "dir": run["dir"],
                "artifact": run["path"],
                "sha256": run["sha256"],
                "evidence": run["evidence"],
                "bounce_settings": run["bounce_settings"],
                "metrics": run["metrics"],
                "guards": run["guards"],
                "comparison": {
                    "before_sha256": incremental["before_sha256"],
                    "after_sha256": incremental["after_sha256"],
                    "before_fingerprint": incremental["before_fingerprint"],
                    "after_fingerprint": incremental["after_fingerprint"],
                    "level_offset_db": incremental["level_offset_db"],
                },
                "overall_comparison": {
                    "before_sha256": overall["before_sha256"],
                    "after_sha256": overall["after_sha256"],
                    "before_fingerprint": overall["before_fingerprint"],
                    "after_fingerprint": overall["after_fingerprint"],
                    "level_offset_db": overall["level_offset_db"],
                },
                "bells": [],
            }
            level_offset = _number(incremental.get("level_offset_db"))
            if level_offset is not None and abs(level_offset) > 2.0:
                self.warnings.append(
                    "spectral level offset %.2f dB in %s"
                    % (level_offset, label))

            track_records = [record for record in records
                             if not record["is_master"]]
            dispositions = []
            unavailable = []
            for record in records:
                inc = _window_effect(incremental["bands"], record["freq_hz"],
                                     record["q"])
                ovr = _window_effect(overall["bands"], record["freq_hz"],
                                     record["q"])
                delta = record["gain"] - record["gain_prev"]
                item = {
                    "band_name": record["band"],
                    "target_key": record["target_key"],
                    "freq_hz": record["freq_hz"],
                    "q": record["q"],
                    "window_octaves": inc["half_width_octaves"],
                    "window_bands": inc["bands"],
                    "G_prev": record["gain_prev"],
                    "G_cur": record["gain"],
                    "G_recipe": record["gain_recipe"],
                    "delta_db": round(delta, 3),
                    "readback": record["readback"],
                    "effect_incremental_db": _round(inc["effect_db"]),
                    "effect_overall_db": _round(ovr["effect_db"]),
                    "deviation_db": _round(
                        None if inc["effect_db"] is None else delta - inc["effect_db"]),
                    "overall_deviation_db": _round(
                        None if ovr["effect_db"] is None else
                        record["gain"] - ovr["effect_db"]),
                    "disposition": None,
                    "correction_db": None,
                }
                if inc["effect_db"] is None or ovr["effect_db"] is None:
                    item["disposition"] = "residual_unavailable"
                    unavailable.append(item)
                elif record["is_master"]:
                    item["disposition"] = "advisory"
                else:
                    item.update(self._disposition(record, inc["effect_db"],
                                                  delta, item["deviation_db"],
                                                  item["overall_deviation_db"]))
                dispositions.append(item)
                entry["bells"].append(item)

            self.iterations.append(entry)
            if unavailable:
                self._stop("residual_unavailable",
                           "no usable residual for band %s in %s"
                           % (unavailable[0]["band_name"], label))

            track_items = [item for item in dispositions
                           if item["disposition"] != "advisory"]
            master_items = [item for item in dispositions
                            if item["disposition"] == "advisory"]
            converged = all(
                item["overall_deviation_db"] is not None
                and abs(item["overall_deviation_db"]) <= self.config.tolerance_db
                for item in track_items)
            master_outside = [
                item for item in master_items
                if item["overall_deviation_db"] is not None
                and abs(item["overall_deviation_db"]) > self.config.tolerance_db
            ]
            if master_outside:
                self.warnings.append(
                    "master EQ effect is advisory (track %s); it is never "
                    "corrected automatically"
                    % ", ".join(item["band_name"] for item in master_outside))

            # Honesty comes before convergence: a band whose window is
            # unavailable, overlapping, or masked by manual dynamics must stop
            # the loop even if its requested gain happens to match the number.
            bad = [item for item in track_items
                   if item["disposition"] in LOW_TRUST_DISPOSITIONS]
            if bad:
                dispositions_order = ("residual_unavailable", "sign_mismatch",
                                      "residual_low_trust", "effect_mismatch")
                for reason in dispositions_order:
                    matching = [item for item in bad
                                if item["disposition"] == reason]
                    if matching:
                        self._stop(reason, "; ".join(
                            "%s on %s" % (item["band_name"], item["target_key"])
                            for item in matching))

            if converged:
                if master_outside:
                    self._stop("master_eq_advisory",
                               "master EQ deviates from its requested gain; "
                               "no readback is available on the output strip")
                self._finish("tolerance")

            deviations = [abs(item["deviation_db"]) for item in track_items
                          if item["deviation_db"] is not None]
            mean_deviation = (sum(deviations) / len(deviations)
                              if deviations else 0.0)
            if previous_mean is not None and \
                    previous_mean - mean_deviation < self.config.no_improvement_db:
                self._stop(
                    "no_improvement",
                    "mean |deviation| did not drop by %.2f dB"
                    % self.config.no_improvement_db)
            previous_mean = mean_deviation

            candidates = [item for item in track_items
                          if item["disposition"] == "correction"]
            if not candidates:
                no_change = all(abs(item["delta_db"]) < 0.5
                                for item in track_items)
                too_small = all(
                    item["effect_incremental_db"] is not None
                    and abs(item["effect_incremental_db"]) < 0.3
                    and abs(item["delta_db"]) >= 0.5
                    for item in track_items)
                if too_small and track_items:
                    self._stop("readback_confirmed",
                               "the requested gain is confirmed by readback "
                               "but the spectrum cannot measure its effect")
                if no_change and not converged:
                    self._stop("effect_mismatch",
                               "every increment landed but the measured sum "
                               "does not match the requested gain")
                self._stop("effect_mismatch",
                           "no band can be corrected without overcorrecting")

            if iteration >= self.config.max_iterations:
                self._stop("max_iterations",
                           "reached %d bell exports" % self.config.max_iterations)

            corrections = self._plan_corrections(candidates, track_items)
            if corrections is None:
                self._stop("correction_cap",
                           "a correction would exceed the GATE or the "
                           "cumulative cap")
            self._apply_corrections(corrections)
            self._readback(records)
            prev_run = run
        self._stop("max_iterations",
                   "reached %d bell exports" % self.config.max_iterations)

    def _disposition(self, record, effect_inc, delta, deviation,
                     overall_deviation) -> dict:
        if record["target_key"] in self.dynamic_targets \
                or "master" in self.dynamic_targets:
            return {"disposition": "residual_low_trust", "correction_db": None}
        if record.get("overlap"):
            return {"disposition": "residual_low_trust", "correction_db": None}
        if abs(delta) < 0.5:
            return {"disposition": "no_change", "correction_db": None}
        if abs(effect_inc) < 0.3:
            return {"disposition": "no_measurable_effect", "correction_db": None}
        ratio = abs(effect_inc) / abs(delta)
        if ratio < 0.25 or ratio > 1.5:
            return {"disposition": "residual_low_trust", "correction_db": None}
        if (effect_inc > 0) != (delta > 0):
            return {"disposition": "sign_mismatch", "correction_db": None}
        if deviation is not None \
                and abs(deviation) <= self.config.incremental_tolerance_db:
            return {"disposition": "landed", "correction_db": None}
        if overall_deviation is None or \
                abs(overall_deviation) <= self.config.tolerance_db:
            return {"disposition": "landed", "correction_db": None}
        return {"disposition": "correction", "correction_db": None}

    def _check_overlap(self, records: list) -> None:
        for record in records:
            record["overlap"] = False
        for index, first in enumerate(records):
            if first["is_master"]:
                continue
            for second in records[index + 1:]:
                if second["is_master"] or second["target_key"] != first["target_key"]:
                    continue
                distance = abs(math.log2(second["freq_hz"] / first["freq_hz"]))
                half = 0.72 / first["q"] + 0.72 / second["q"]
                if distance < half:
                    first["overlap"] = second["overlap"] = True

    def _plan_corrections(self, candidates: list, track_items: list):
        proposed = {}
        for item in candidates:
            deviation = item["deviation_db"]
            if deviation is None:
                return None
            step = max(-self.config.max_correction_db,
                       min(self.config.max_correction_db, deviation))
            proposed[(item["target_key"], item["band_name"])] = \
                item["G_cur"] + step
        total = 0.0
        for item in track_items:
            key = (item["target_key"], item["band_name"])
            value = proposed.get(key, item["G_cur"])
            if abs(value) > rules.GATE["max_corrective_db_per_point"]:
                return None
            total += abs(value)
            recipe_gain = next(
                (record["gain_recipe"] for record in self.bell_records
                 if (record["target_key"], record["band"]) == key), None)
            if recipe_gain is not None and \
                    abs(value - recipe_gain) > self.config.max_cumulative_correction_db:
                return None
        if total > rules.GATE["max_cumulative_corrective_db"]:
            return None
        corrections = []
        for item in candidates:
            key = (item["target_key"], item["band_name"])
            corrections.append({
                "target_key": item["target_key"],
                "band": item["band_name"],
                "gain": proposed[key],
                "previous": item["G_cur"],
                "delta": proposed[key] - item["G_cur"],
            })
        return corrections

    def _apply_corrections(self, corrections: list) -> None:
        steps = []
        for correction in corrections:
            record = next(
                (entry for entry in self.bell_records
                 if entry["target_key"] == correction["target_key"]
                 and entry["band"] == correction["band"]), None)
            if record is None:
                self._stop("plan_blocked", "correction has no planning record")
            params = {
                "band": correction["band"],
                "insert": record["insert"],
                "parameter": "Gain",
                "value": correction["gain"],
                "unit": "dB",
                "mode": MODE,
                "project_expected_path": self.config.project,
            }
            if record["ref"]:
                params["target_ref"] = record["ref"]
            elif record["is_master"]:
                params["strip"] = contract.MASTER_STRIP
            else:
                params["track"] = record["track"]
            call = {
                "tool": contract.TOOL_PLUGINS,
                "arguments": {"command": "set_eq_band_verified",
                              "params": params},
                "kind": "write",
                "requires_insert": False,
                "target_key": record["target_key"],
                "insert": record["insert"],
                "band": record["band"],
            }
            steps.append({
                "action": "eq.bell",
                "phase": "bell",
                "status": "ready",
                "target_key": record["target_key"],
                "target_selector": record["target_selector"],
                "calls": [call],
            })
        self._apply_phase(steps, "bell")
        for correction in corrections:
            record = next(
                (entry for entry in self.bell_records
                 if entry["target_key"] == correction["target_key"]
                 and entry["band"] == correction["band"]), None)
            record["gain_prev"] = correction["previous"]
            record["gain"] = correction["gain"]
            record["corrections"].append({
                "from_db": correction["previous"],
                "to_db": correction["gain"],
                "delta_db": correction["delta"],
            })

    # -- comparisons -------------------------------------------------------
    def _compare_runs(self, before: dict, after: dict) -> dict:
        params = {
            "before_path": before["path"],
            "after_path": after["path"],
            "output_root": self.run_root,
            "comparison_mode": "spectral_shape",
        }
        result = self._bridge_call(
            contract.TOOL_AUDIO, "compare_spectra", params,
            mcp_client.ANALYSIS_TIMEOUT, "analysis_failed")
        payload = _payload(result, "analysis_failed")
        if payload.get("complete") is not True:
            self._stop("analysis_failed", "compare_spectra is incomplete")
        shape = payload.get("shapeComparison")
        if not isinstance(shape, dict) or shape.get("complete") is not True:
            self._stop("analysis_failed",
                       "shapeComparison is missing or incomplete")
        before_fp = _fingerprint(payload, "before")
        after_fp = _fingerprint(payload, "after")
        expected_before = "sha256:%s" % before["sha256"]
        expected_after = "sha256:%s" % after["sha256"]
        if not before_fp or before_fp.lower() != expected_before.lower():
            self._stop("artifact_changed",
                       "before fingerprint does not match the local SHA-256")
        if not after_fp or after_fp.lower() != expected_after.lower():
            self._stop("artifact_changed",
                       "after fingerprint does not match the local SHA-256")
        level_offset = shape.get("levelOffsetDb")
        if not _is_number(level_offset):
            level_offset = payload.get("levelOffsetDb")
        return {
            "bands": _residual_bands(payload),
            "before_sha256": before["sha256"],
            "after_sha256": after["sha256"],
            "before_fingerprint": before_fp,
            "after_fingerprint": after_fp,
            "level_offset_db": _number(level_offset),
        }

    # -- termination / report ---------------------------------------------
    def _finish(self, reason: str) -> None:
        if self.manual_delivery and reason in ("tolerance", "static_only",
                                               "readback_confirmed"):
            self._stop("manual_delivery",
                       "the limiter is manual; true peak exceeds the requested "
                       "ceiling")
        self._stop(reason)

    def _exit_code(self) -> int:
        reason = self.stop_reason
        if reason in FAILURE_REASONS:
            return 1
        if reason in HONEST_REASONS:
            return 2
        if self.manual_steps or self.master_eq_written or self.manual_delivery:
            return 2
        return 0

    def _report(self) -> dict:
        reason = self.stop_reason
        if reason in SUCCESS_REASONS and self.manual_delivery:
            self.stop_reason = "manual_delivery"
        if self.manual_steps:
            self.warnings.append("manual_actions_pending")
        report = {
            "schema": LOOP_SCHEMA,
            "dry_run": not self.config.apply,
            "run_id": self.run_id,
            "run_root": self.run_root,
            "project": self.config.project,
            "recipe_sha256": _canonical_recipe_sha(self.config.recipe),
            "adapter_contract_version": contract.ADAPTER_CONTRACT_VERSION,
            "baseline_source": (self.baseline or {}).get("source"),
            "baseline": self.baseline,
            "stage": self.stage,
            "exports": self.exported_runs,
            "iterations": self.iterations,
            "plan": self._plan_payload(),
            "operations": self.operations,
            "manual": [{"action": step.get("action"),
                        "target": step.get("target"),
                        "target_key": step.get("target_key"),
                        "reason": step.get("reason"),
                        "hint": step.get("hint")}
                       for step in self.manual_steps],
            "warnings": _dedupe(self.warnings),
            "stop_reason": self.stop_reason,
            "exit": self._exit_code(),
        }
        if self.stop_detail:
            report["detail"] = self.stop_detail
        if not self.config.apply:
            # A preflight failure can stop the run before a run root exists;
            # the dry-run report must still be JSON instead of a traceback.
            layout = {}
            if self.run_root:
                layout = {"baseline": os.path.join(self.run_root, "baseline")}
                static = [step for step in self.steps
                          if step.get("phase") == "static"
                          and step.get("calls")]
                if static:
                    layout["stage"] = os.path.join(self.run_root, "stage")
                if any(step.get("phase") == "bell" for step in self.steps):
                    for number in range(1, self.config.max_iterations + 1):
                        layout["iter-%d" % number] = os.path.join(
                            self.run_root, "iter-%d" % number)
                report["export_plan_preview"] = self._export_params(
                    layout["baseline"])
            report["layout"] = layout
        if self.report_written:
            report["report_path"] = self.report_written
        return report

    def _write_report(self, report: dict) -> None:
        path = os.path.abspath(self.config.report_path)
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=1, ensure_ascii=False,
                          default=str)
            self.report_written = path
            report["report_path"] = path
        except OSError as exc:
            report["warnings"].append("cannot write the report: %s" % exc)
            report["exit"] = 1


def _round(value):
    return round(float(value), 3) if value is not None else None


def _dedupe(values):
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def run_loop(config: LoopConfig, **kwargs) -> dict:
    return LoopRunner(config, **kwargs).run()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _read_recipe(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        recipe = json.load(handle)
    if not isinstance(recipe, list):
        raise ValueError("recipe must be a JSON list")
    return recipe


def _metadata_reader(endpoint: str):
    def read(path: str) -> dict:
        result = mcp_client.call_tool(
            endpoint, contract.TOOL_AUDIO,
            {"command": "analyze_file", "params": {"path": path}},
            timeout=mcp_client.ANALYSIS_TIMEOUT)
        return {"ok": result.ok, "payload": result.payload,
                "error": result.error}
    return read


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="adapter.loop")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--project", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--tracks", default="")
    parser.add_argument("--master", default="")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--before", default=None)
    parser.add_argument("--trust-before", action="store_true")
    parser.add_argument("--fader-calibration", default=None)
    parser.add_argument("--overwrite-eq", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--tolerance-db", type=float, default=1.0)
    parser.add_argument("--incremental-tolerance-db", type=float, default=0.5)
    parser.add_argument("--max-correction-db", type=float, default=1.0)
    parser.add_argument("--max-cumulative-correction-db", type=float, default=3.0)
    parser.add_argument("--no-improvement-db", type=float, default=0.2)
    parser.add_argument("--allow-unverified-bounce-settings",
                        action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)

    try:
        recipe = _read_recipe(args.recipe)
        tracks = _parse_targets(args.tracks)
        master = _parse_master(args.master)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=1))
        return 1

    config = LoopConfig(
        project=args.project, recipe=recipe, output_root=args.output_root,
        tracks=tracks, master=master, endpoint=args.endpoint,
        run_id=args.run_id, before=args.before, trust_before=args.trust_before,
        fader_calibration=FaderCalibration.load(args.fader_calibration),
        overwrite_eq=args.overwrite_eq, max_iterations=args.max_iterations,
        tolerance_db=args.tolerance_db,
        incremental_tolerance_db=args.incremental_tolerance_db,
        max_correction_db=args.max_correction_db,
        max_cumulative_correction_db=args.max_cumulative_correction_db,
        no_improvement_db=args.no_improvement_db,
        allow_unverified_bounce_settings=args.allow_unverified_bounce_settings,
        apply=args.apply, report_path=args.report)
    report = run_loop(config, metadata_reader=_metadata_reader(args.endpoint))
    print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    return int(report.get("exit", 1))


if __name__ == "__main__":
    sys.exit(main())
