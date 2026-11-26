"""The run store and the step cache: resume after a crash with zero repeated calls, free replay, stop markers."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message, TextBlock, Usage

from anbud.config import load_config
from anbud.model_client import Budget, ModelClient, ReplayMiss, RunStopped
from anbud.models import Event
from anbud.store import RunStore, StoreError, request_stop

SYSTEM = [{"type": "text", "text": "Synthetic system block."}]


def _message(text: str) -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        content=[TextBlock(type="text", text=text)],
        model="claude-opus-5-5",
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=1000, output_tokens=100),
    )


class Killed(BaseException):
    """Stands in for the process dying mid-run."""


class _Stream:
    def __init__(self, reply: Message) -> None:
        self.reply = reply
        self.request_id = "req_stream"

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Message:
        return self.reply


class FakeSDK:
    """Answers every request with a reply naming the question. `kill_at` dies on that call, unsent."""

    def __init__(self, kill_at: int | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.kill_at = kill_at
        self.messages = self

    def count_tokens(self, **params: Any) -> Any:
        return SimpleNamespace(input_tokens=1000)

    def stream(self, **params: Any) -> _Stream:
        if self.kill_at is not None and len(self.sent) + 1 == self.kill_at:
            raise Killed
        self.sent.append(params)
        return _Stream(_message(f"reply to {params['messages'][-1]['content']}"))


def _client(store: RunStore, sdk: Any, run_id: str = "run-1", **kw: Any) -> ModelClient:
    return ModelClient(load_config(), sdk, Budget(5.0), run_id=run_id, company_id=store.company_id, store=store, **kw)


def _ask(client: ModelClient, question: str) -> Any:
    messages = [{"role": "user", "content": question}]
    return client.call(role="map", step="s", system=SYSTEM, messages=messages, max_tokens=64, effort="low")


def test_blobs_are_content_addressed_private_and_checked(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    sha = store.put_blob(b"synthetic bytes")
    assert store.put_blob(b"synthetic bytes") == sha and store.get_blob(sha) == b"synthetic bytes"
    path = store.dir / "blobs" / sha[:2] / sha
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    path.write_bytes(b"tampered")
    with pytest.raises(StoreError):
        store.get_blob(sha)


def test_the_store_never_lives_in_sealed_storage(tmp_path: Path) -> None:
    (tmp_path / "heldout" / "inner").mkdir(parents=True)
    os.symlink(tmp_path / "heldout" / "inner", tmp_path / "co-998")
    with pytest.raises(StoreError):
        RunStore("co-998", root=tmp_path)


def test_events_append_and_stay_with_their_company(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    event = Event(run_id="run-1", company_id="co-999", step="s", kind="start", at=datetime.now(UTC))
    store.append_event(event)
    store.append_event(event)
    assert store.events("run-1") == [event, event]
    with pytest.raises(StoreError):
        store.append_event(event.model_copy(update={"company_id": "co-998"}))
    with pytest.raises(StoreError):
        store.run_dir("../escape")


def test_a_run_keeps_its_salt(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    salt = store.open_run("run-1")
    assert store.open_run("run-1") == salt and len(salt) == 32
    with pytest.raises(StoreError):
        store.open_run("run-1", salt="another")


def test_a_repeated_request_is_answered_from_the_cache_at_no_cost(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    sdk = FakeSDK()
    client = _client(store, sdk)
    first = _ask(client, "q1")
    again = _ask(client, "q1")
    assert len(sdk.sent) == 1
    assert again.cached and again.cost_usd == 0 and again.request_id == "req_stream"
    assert again.message.model_dump() == first.message.model_dump()  # byte-identical for downstream code
    assert client.budget.spent == pytest.approx(first.cost_usd)
    kinds = [e.kind for e in store.events("run-1")]
    assert kinds == ["model_call", "cache_hit"]
    call, hit = store.events("run-1")
    assert call.blob == hit.blob and call.request_blob == hit.request_blob and call.blob is not None


def test_the_cache_is_per_salt_and_per_company(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    sdk = FakeSDK()
    _ask(_client(store, sdk, run_id="run-1"), "q1")
    _ask(_client(store, sdk, run_id="run-2"), "q1")  # a new run gets a new salt: a fresh sample
    other = RunStore("co-998", root=tmp_path)
    _ask(_client(other, sdk, run_id="run-1", salt=store.open_run("run-1")), "q1")  # same salt, other company
    assert len(sdk.sent) == 3


def test_a_killed_run_resumes_without_repeating_a_call(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    questions = [f"q{i}" for i in range(1, 6)]
    first = FakeSDK(kill_at=4)
    with pytest.raises(Killed):
        client = _client(store, first)
        for question in questions:
            _ask(client, question)
    assert len(first.sent) == 3
    second = FakeSDK()
    client = _client(store, second)  # same run id: same salt
    results = [_ask(client, question) for question in questions]
    assert [p["messages"][-1]["content"] for p in second.sent] == ["q4", "q5"]
    assert [r.cached for r in results] == [True, True, True, False, False]


def test_replay_answers_from_the_cache_and_never_calls_a_model(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    _ask(_client(store, FakeSDK()), "q1")
    replay = _client(store, None)
    assert _ask(replay, "q1").cached
    with pytest.raises(ReplayMiss):
        _ask(replay, "q2")


def test_a_stop_marker_stops_the_run_before_a_call(tmp_path: Path) -> None:
    store = RunStore("co-999", root=tmp_path)
    sdk = FakeSDK()
    client = _client(store, sdk)
    request_stop("co-999", "run-1", root=tmp_path)
    with pytest.raises(RunStopped):
        _ask(client, "q1")
    request_stop("co-999", "run-1", root=tmp_path, clear=True)
    request_stop(None, None, root=tmp_path)  # every run
    with pytest.raises(RunStopped):
        _ask(client, "q1")
    request_stop(None, None, root=tmp_path, clear=True)
    _ask(client, "q1")
    assert len(sdk.sent) == 1


def test_the_stop_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from anbud import cli

    monkeypatch.setenv("ANBUD_DATA_ROOT", str(tmp_path))
    assert cli.main(["stop", "co-999", "run-1"]) == 0
    assert RunStore("co-999", root=tmp_path).stop_requested("run-1")
    assert cli.main(["stop", "co-999", "run-1", "--clear"]) == 0
    assert not RunStore("co-999", root=tmp_path).stop_requested("run-1")
    assert cli.main(["stop", "--all"]) == 0 and (tmp_path / "STOP").exists()
    assert cli.main(["stop"]) == cli.REFUSED
    assert cli.main(["stop", "acme", "run-1"]) == cli.REFUSED  # company ids look like co-001
