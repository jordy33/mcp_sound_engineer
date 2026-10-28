"""Jev client: .env parsing, endpoint policy, transport errors and retries."""
import email.message
import http.client
import io
import json
import os
import sys
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import jev  # noqa: E402
import rules  # noqa: E402

CFG = {"key": "test-key", "endpoint": "https://api.example.test/decide",
       "model": "test-model"}
ANSWERS = {"low_end": {"type": "score", "score": 4.0, "noul": 0.1}}


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: bytes = b"error", retry_after: str | None = None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://api.example.test/decide", code,
                                  "error", headers, io.BytesIO(body))


def _ask_with(side_effect, sleeps=None):
    patches = [mock.patch.object(jev, "config", return_value=dict(CFG)),
               mock.patch.object(jev.urllib.request, "urlopen", side_effect=side_effect)]
    if sleeps is not None:
        patches.append(mock.patch.object(jev.time, "sleep",
                                         side_effect=lambda s: sleeps.append(s)))
    with patches[0], patches[1]:
        with (patches[2] if sleeps is not None else mock.patch.object(jev.time, "sleep")):
            return jev.ask({"facts": {}}, {"low_end": {"type": "score"}})


# --- .env parsing -----------------------------------------------------------
def test_env_parser_handles_comments_export_and_quotes():
    assert jev._parse_env_line("A=1 # inline comment") == ("A", "1")
    assert jev._parse_env_line('export B="two # inside"') == ("B", "two # inside")
    assert jev._parse_env_line("C='x=y'   # trailing") == ("C", "x=y")
    assert jev._parse_env_line('D="quoted" # out') == ("D", "quoted")
    assert jev._parse_env_line("E=value#comment") == ("E", "value")
    assert jev._parse_env_line("# whole line") is None
    assert jev._parse_env_line("NOEQUALS") is None
    assert jev._parse_env_line("   ") is None


def test_env_file_is_read_before_the_process_environment():
    with mock.patch.object(jev, "_load_env",
                           return_value={"JEV_API_KEY": "jv_live_abc"}):
        endpoint = jev.config()["endpoint"]
    assert endpoint == jev.JEV_LIVE_ENDPOINT, endpoint


# --- endpoint policy --------------------------------------------------------
def test_endpoint_by_key_prefix_and_explicit_override():
    assert jev._default_endpoint("jv_live_abc") == jev.JEV_LIVE_ENDPOINT
    assert jev._default_endpoint("apikey_abc") == jev.DEFAULT_ENDPOINT
    assert jev._default_endpoint("other") == jev.DEFAULT_ENDPOINT
    with mock.patch.object(jev, "_load_env",
                           return_value={"JEV_API_KEY": "jv_live_abc",
                                         "JEV_ENDPOINT": "https://local.example/x"}):
        assert jev.config()["endpoint"] == "https://local.example/x"


def test_http_is_only_allowed_on_loopback():
    for url in ("http://127.0.0.1:8080/decide", "http://localhost/decide",
                "http://[::1]:9000/decide", "https://api.example.test/decide"):
        assert jev._validate_endpoint(url) == url
    for url in ("http://evil.example/decide", "ftp://127.0.0.1/x",
                "not-a-url", "https:///missing-host"):
        try:
            jev._validate_endpoint(url)
        except jev.JevError:
            pass
        else:
            raise AssertionError(f"{url!r} must be rejected")


# --- transport --------------------------------------------------------------
def test_ask_returns_the_answers_map():
    response = _Response(json.dumps({"answers": ANSWERS}).encode())
    assert _ask_with([response]) == ANSWERS


def test_timeout_becomes_a_domain_error():
    try:
        _ask_with([TimeoutError("slow")])
    except jev.JevError as exc:
        assert "request failed" in str(exc), exc
    else:
        raise AssertionError("a timeout must not escape as TimeoutError")


def test_invalid_json_becomes_a_domain_error():
    try:
        _ask_with([_Response(b"not json")])
    except jev.JevError as exc:
        assert "invalid JSON" in str(exc), exc
    else:
        raise AssertionError("invalid JSON must become a JevError")


def test_protocol_error_becomes_a_domain_error():
    try:
        _ask_with([http.client.RemoteDisconnected("gone")])
    except jev.JevError as exc:
        assert "protocol error" in str(exc), exc
    else:
        raise AssertionError("an HTTPException must become a JevError")


def test_unreachable_host_becomes_a_domain_error():
    try:
        _ask_with([urllib.error.URLError("dns")])
    except jev.JevError as exc:
        assert "unreachable" in str(exc), exc
    else:
        raise AssertionError("a URLError must become a JevError")


# --- retries ----------------------------------------------------------------
def test_retries_429_and_5xx_and_honors_retry_after():
    sleeps = []
    response = _Response(json.dumps({"answers": ANSWERS}).encode())
    result = _ask_with([_http_error(429, retry_after="1.5"),
                        _http_error(503), response], sleeps)
    assert result == ANSWERS
    assert sleeps == [1.5, 1.0], sleeps


def test_never_retries_a_client_error():
    calls = []

    def side_effect(*args, **kwargs):
        calls.append(1)
        raise _http_error(400, b"bad request")

    try:
        _ask_with(side_effect)
    except jev.JevError as exc:
        assert "HTTP 400" in str(exc) and "bad request" in str(exc), exc
    else:
        raise AssertionError("a 400 must raise without retrying")
    assert len(calls) == 1, calls


def test_retry_after_wait_is_capped():
    sleeps = []
    response = _Response(json.dumps({"answers": ANSWERS}).encode())
    _ask_with([_http_error(429, retry_after="9999"), response], sleeps)
    assert sleeps == [jev.RETRY_MAX_DELAY_S], sleeps


# --- response validation ----------------------------------------------------
def test_require_rejects_answers_outside_the_schema():
    questions = {"low_end": {"type": "score"}}
    for bad in (0.0, 12.0, float("nan"), "heavy", None):
        try:
            rules.require({"low_end": {"score": bad}}, questions)
        except ValueError:
            pass
        else:
            raise AssertionError(f"score {bad!r} must be rejected")
    with_bad_choice = {"balance": {"type": "choice",
                                   "criteria": {"balanced": "", "bass_too_heavy": ""}}}
    try:
        rules.require({"balance": {"choice": "boost"}}, with_bad_choice)
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown choice must be rejected")
    for field, value in (("noul", 1.5), ("noul", -0.1), ("confidence", 2.0)):
        try:
            rules.require({"low_end": {"score": 4.0, field: value}}, questions)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{field}={value} must be rejected")


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
