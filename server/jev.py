"""Minimal Jev (TypeSafe AI) client: typed decisions over a state."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevError(RuntimeError):
    pass


def _load_env() -> dict:
    """Read .env from the repo root, then the real environment (env wins)."""
    values: dict[str, str] = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if os.path.isfile(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    values.update({k: os.environ[k] for k in
                   ("JEV_API_KEY", "JEV_ENDPOINT", "JEV_MODEL") if k in os.environ})
    return values


def config() -> dict:
    env = _load_env()
    return {
        "key": env.get("JEV_API_KEY", ""),
        "endpoint": env.get("JEV_ENDPOINT", DEFAULT_ENDPOINT),
        "model": env.get("JEV_MODEL", "jev-latest"),
    }


def ask(state: dict, questions: dict, timeout: int = 60) -> dict:
    """Send one decision request. Returns the `answers` map."""
    cfg = config()
    if not cfg["key"]:
        raise JevError("JEV_API_KEY is not set (copy .env.example to .env)")
    body = {"model": cfg["model"], "state": state, "questions": questions}
    req = urllib.request.Request(
        cfg["endpoint"], data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise JevError(f"Jev HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise JevError(f"Jev unreachable: {exc.reason}") from exc
    if "answers" not in payload:
        raise JevError(f"unexpected Jev response: {str(payload)[:300]}")
    return payload["answers"]
