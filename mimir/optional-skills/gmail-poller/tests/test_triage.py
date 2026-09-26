"""Jev pre-turn triage tests for the gmail poller."""
from __future__ import annotations

import importlib
import io
import json
import socket
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest


@pytest.fixture
def fresh_poller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("POLLER_NAME", "gmail-inbox")
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.delenv("JEV_KEY", raising=False)
    sys.modules.pop("poller", None)
    return importlib.import_module("poller")


def _message(**overrides) -> dict:
    message = {
        "id": "m1",
        "threadId": "thread-1",
        "from": "Deals <deals@shop-example.com>",
        "subject": "48-hour flash sale",
        "snippet": "Don't miss out.",
    }
    message.update(overrides)
    return message


def _questions() -> dict:
    return {
        "notify": {
            "type": "noul",
            "instructions": "Should the account owner be notified?",
            "criteria": {
                "true": "needs the owner's attention",
                "false": "safe to skip silently",
            },
        }
    }


def _configure(tmp_path: Path, *, triage: object, prompt: str = "Account rules") -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "home",
                        "email": "owner@example.com",
                        "prompt": prompt,
                        "triage": triage,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _triage_config(**overrides) -> dict:
    triage = {
        "model": "jev-1.13.0",
        "questions": _questions(),
        "drop_below": 0.10,
        "always_emit": [],
    }
    triage.update(overrides)
    return triage


def _response(notify: object = 0.06, **overrides) -> dict:
    response = {
        "model": "jev-1.13.0",
        "answers": {"notify": {"type": "noul", "noul": notify}},
        "usage": {"input_tokens": 465, "output_tokens": 68},
    }
    response.update(overrides)
    return response


class _FakeResponse:
    def __init__(self, body: object):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def _run(
    poller,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    *,
    message: dict | None = None,
) -> tuple[list[dict], str]:
    monkeypatch.setattr(poller, "_gog_search", lambda *_args: [message or _message()])
    assert poller.main() == 0
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line]
    return events, captured.err


def test_no_triage_preserves_event_and_makes_no_request(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    _configure(tmp_path, triage=None)
    config = json.loads((tmp_path / "config.json").read_text())
    del config["accounts"][0]["triage"]
    (tmp_path / "config.json").write_text(json.dumps(config))

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("an account without triage must not contact Jev")

    monkeypatch.setattr(fresh_poller.request, "urlopen", unexpected_request)
    events, _ = _run(fresh_poller, monkeypatch, capsys)

    assert events == [
        {
            "poller": "gmail-inbox",
            "prompt": (
                "[gmail] new message from Deals <deals@shop-example.com>: "
                "'48-hour flash sale'\n  > Don't miss out.\n"
                "  URL: https://mail.google.com/mail/u/0/#inbox/thread-1\n"
                "  message_id: m1\n\nAccount rules"
            ),
            "source_platform": "gmail",
            "message_id": "m1",
            "thread_id": "thread-1",
            "from": "Deals <deals@shop-example.com>",
            "subject": "48-hour flash sale",
            "snippet": "Don't miss out.",
            "url": "https://mail.google.com/mail/u/0/#inbox/thread-1",
            "account": "owner@example.com",
            "account_name": "home",
        }
    ]


def test_confident_skip_is_audited_and_cursored(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    _configure(tmp_path, triage=_triage_config())
    monkeypatch.setenv("JEV_KEY", "test-key")
    monkeypatch.setattr(
        fresh_poller.request, "urlopen", lambda *_a, **_k: _FakeResponse(_response())
    )

    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert events == []
    assert json.loads(fresh_poller.CURSOR_FILE.read_text()) == ["m1"]
    records = [
        json.loads(line)
        for line in fresh_poller.TRIAGE_DROPPED_FILE.read_text().splitlines()
    ]
    assert records == [
        {
            "message_id": "m1",
            "url": "https://mail.google.com/mail/u/0/#inbox/thread-1",
            "from": "Deals <deals@shop-example.com>",
            "subject": "48-hour flash sale",
            "answers": {"notify": {"type": "noul", "noul": 0.06}},
            "model": "jev-1.13.0",
        }
    ]
    assert "dropped=1" in stderr


@pytest.mark.parametrize("noul, dropped", [(0.10, True), (0.11, False), (0.91, False)])
def test_noul_threshold_is_inclusive(
    fresh_poller, tmp_path, monkeypatch, capsys, noul, dropped,
):
    _configure(tmp_path, triage=_triage_config())
    monkeypatch.setenv("JEV_KEY", "test-key")
    monkeypatch.setattr(
        fresh_poller.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(_response(noul)),
    )

    events, _ = _run(fresh_poller, monkeypatch, capsys)

    if dropped:
        assert events == []
        assert fresh_poller.TRIAGE_DROPPED_FILE.exists()
    else:
        assert events[0]["triage"] == {
            "model": "jev-1.13.0",
            "answers": {"notify": {"type": "noul", "noul": noul}},
        }
        assert "Jev triage answers:" in events[0]["prompt"]
        assert not fresh_poller.TRIAGE_DROPPED_FILE.exists()


@pytest.mark.parametrize("always_emit", [["family@example.com"], ["example.com"]])
def test_always_emit_bypasses_jev(
    fresh_poller, tmp_path, monkeypatch, capsys, always_emit,
):
    _configure(tmp_path, triage=_triage_config(always_emit=always_emit))
    monkeypatch.setenv("JEV_KEY", "test-key")

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("always_emit sender must bypass Jev, including its snippet")

    monkeypatch.setattr(fresh_poller.request, "urlopen", unexpected_request)
    events, _ = _run(
        fresh_poller,
        monkeypatch,
        capsys,
        message=_message(**{"from": "Family <family@example.com>"}, snippet="private"),
    )

    assert len(events) == 1
    assert "triage" not in events[0]


def test_request_contains_only_triage_fields_and_message_preview(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    questions = _questions()
    questions["category"] = {
        "type": "choice",
        "instructions": "What kind of email is this?",
        "criteria": {"marketing": "A promotion", "other": "Anything else"},
    }
    _configure(tmp_path, triage=_triage_config(questions=questions))
    monkeypatch.setenv("JEV_KEY", "test-key")
    captured = {}

    def fake_urlopen(req, *, timeout):
        captured.update(
            body=json.loads(req.data),
            authorization=req.get_header("Authorization"),
            content_type=req.get_header("Content-type"),
            method=req.get_method(),
            timeout=timeout,
        )
        response = _response(0.91)
        response["answers"]["category"] = {
            "type": "choice",
            "choice": "marketing",
            "confidence": 1.0,
            "probabilities": {"marketing": 1.0, "other": 0.0},
        }
        return _FakeResponse(response)

    monkeypatch.setattr(fresh_poller.request, "urlopen", fake_urlopen)
    events, _ = _run(
        fresh_poller,
        monkeypatch,
        capsys,
        message=_message(
            internalDate="secret timestamp",
            labels=["SECRET"],
            body="full body must not be sent",
        ),
    )

    assert len(events) == 1
    assert captured == {
        "body": {
            "model": "jev-1.13.0",
            "state": (
                "From: Deals <deals@shop-example.com>\n"
                "Subject: 48-hour flash sale\nSnippet: Don't miss out."
            ),
            "questions": questions,
        },
        "authorization": "Bearer test-key",
        "content_type": "application/json",
        "method": "POST",
        "timeout": 5.0,
    }


@pytest.mark.parametrize("status", [500, 422, 429])
def test_http_errors_fail_open_once(
    fresh_poller, tmp_path, monkeypatch, capsys, status,
):
    _configure(tmp_path, triage=_triage_config())
    monkeypatch.setenv("JEV_KEY", "test-key")

    def fail(req, **_kwargs):
        raise HTTPError(req.full_url, status, "failure", {}, io.BytesIO())

    monkeypatch.setattr(fresh_poller.request, "urlopen", fail)
    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert len(events) == 1 and "triage" not in events[0]
    assert stderr.count("triage failed") == 1
    assert not fresh_poller.TRIAGE_DROPPED_FILE.exists()


@pytest.mark.parametrize(
    "failure",
    [socket.timeout("slow"), _FakeResponse(b"not json")],
    ids=["timeout", "malformed-body"],
)
def test_transport_and_body_errors_fail_open_once(
    fresh_poller, tmp_path, monkeypatch, capsys, failure,
):
    _configure(tmp_path, triage=_triage_config())
    monkeypatch.setenv("JEV_KEY", "test-key")

    def fail(*_args, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(fresh_poller.request, "urlopen", fail)
    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert len(events) == 1 and "triage" not in events[0]
    assert stderr.count("triage failed") == 1
    assert not fresh_poller.TRIAGE_DROPPED_FILE.exists()


def test_missing_key_fails_open_once(fresh_poller, tmp_path, monkeypatch, capsys):
    _configure(tmp_path, triage=_triage_config())

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("missing key must not make a request")

    monkeypatch.setattr(fresh_poller.request, "urlopen", unexpected_request)
    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert len(events) == 1 and "triage" not in events[0]
    assert stderr.count("triage failed") == 1
    assert "JEV_KEY is missing" in stderr


@pytest.mark.parametrize(
    "notify",
    [
        {"noul": 0.06},
        {"type": "choice", "choice": "skip", "confidence": 1.0},
        {"type": "noul", "noul": "0.06"},
        {"type": "noul", "noul": -0.1},
        {"type": "noul", "noul": 0.06, "confidence": 1.0},
    ],
    ids=["missing-type", "wrong-type", "not-number", "out-of-range", "unknown-field"],
)
def test_unknown_notify_shape_fails_open(
    fresh_poller, tmp_path, monkeypatch, capsys, notify,
):
    _configure(tmp_path, triage=_triage_config())
    monkeypatch.setenv("JEV_KEY", "test-key")
    monkeypatch.setattr(
        fresh_poller.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(
            {"model": "jev-1.13.0", "answers": {"notify": notify}}
        ),
    )

    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert len(events) == 1 and "triage" not in events[0]
    assert stderr.count("triage failed") == 1
    assert not fresh_poller.TRIAGE_DROPPED_FILE.exists()


@pytest.mark.parametrize(
    "triage",
    [
        None,
        _triage_config(
            questions={
                "notify": {"type": "noul", "instructions": "x"},
                "unknown": {"type": "mystery", "instructions": "x"},
            }
        ),
        _triage_config(
            questions={
                "notify": {"type": "noul", "instructions": "x"},
                "kind": {"type": "choice", "instructions": "x", "criteria": []},
            }
        ),
        _triage_config(
            questions={
                "notify": {"type": "noul", "instructions": "x"},
                "risk": {"type": "score", "instructions": "x", "criteria": {"1": "low"}},
            }
        ),
        _triage_config(questions={"notify": {"type": "noul"}}),
        _triage_config(drop_below=1.01),
        _triage_config(questions={"other": {"type": "noul", "instructions": "x"}}),
        _triage_config(
            questions={
                "notify": {
                    "type": "choice",
                    "instructions": "x",
                    "criteria": {"yes": "yes", "no": "no"},
                }
            }
        ),
    ],
    ids=[
        "not-an-object",
        "unknown-type",
        "choice-criteria-list",
        "score-criteria-object",
        "missing-instructions",
        "threshold-out-of-range",
        "missing-notify",
        "notify-not-noul",
    ],
)
def test_invalid_config_disables_triage_once(
    fresh_poller, tmp_path, monkeypatch, capsys, triage,
):
    _configure(tmp_path, triage=triage)
    monkeypatch.setenv("JEV_KEY", "test-key")

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("invalid triage configuration must not make a request")

    monkeypatch.setattr(fresh_poller.request, "urlopen", unexpected_request)
    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert len(events) == 1 and "triage" not in events[0]
    assert stderr.count("invalid triage configuration") == 1


def test_audit_failure_emits_instead_of_dropping(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    _configure(tmp_path, triage=_triage_config())
    monkeypatch.setenv("JEV_KEY", "test-key")
    monkeypatch.setattr(
        fresh_poller.request, "urlopen", lambda *_a, **_k: _FakeResponse(_response())
    )
    monkeypatch.setattr(fresh_poller, "_audit_drop", lambda *_args: False)

    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert len(events) == 1
    assert events[0]["triage"]["answers"]["notify"]["noul"] == 0.06
    assert "dropped=0" in stderr


def test_audit_write_error_fails_open(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    _configure(tmp_path, triage=_triage_config())
    monkeypatch.setenv("JEV_KEY", "test-key")
    monkeypatch.setattr(
        fresh_poller.request, "urlopen", lambda *_a, **_k: _FakeResponse(_response())
    )
    audit_directory = tmp_path / "cannot-append-as-jsonl"
    audit_directory.mkdir()
    monkeypatch.setattr(fresh_poller, "TRIAGE_DROPPED_FILE", audit_directory)

    events, stderr = _run(fresh_poller, monkeypatch, capsys)

    assert len(events) == 1
    assert "triage audit failed" in stderr
    assert "dropped=0" in stderr


def test_manifest_passes_jev_key():
    manifest_path = Path(__file__).resolve().parent.parent / "pollers.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "JEV_KEY" in manifest["pollers"][0]["pass_env"]
