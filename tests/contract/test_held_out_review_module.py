"""Coldline.

===================

File:              tests/contract/test_held_out_review_module.py
Component:         Contract — Held-out review dry run
Purpose:           Confirm the held-out check mechanism works, using a fake, non-secret scenario.
Interacts With:    tests.contract.held_out_review
Sprint/Task:       Sprint 3 — Project 3 / Task 3.6
Concepts:          Held-out evaluation
Tools:             Python 3.12, pytest, httpx

Unlike Task 2.8's dry run, this one needs no running stack: the procedure's Docker,
queue, clock, and HTTP seams are replaced with one in-memory stand-in, so the *mechanism*
- pass, failed grade, unusable run, and a content-free output - is provable in `poe
contract` without stopping a real container. The real fault runs only in the protected
job, against the real stack, with the real scenario.
"""

import itertools
import json
from collections.abc import Iterator
from typing import Any, Literal, Self

import httpx
import pytest

from tests.contract import held_out_review
from tests.contract.held_out_review import (
    FAIL,
    INFRASTRUCTURE,
    PASS,
    ScenarioError,
    main,
    run_held_out_check,
)

# A fake scenario, in the open, so the mechanism is testable without the real one. Its
# numbers are invented for this check and grade nothing: the protected CI job supplies
# the real scenario at runtime.
FAKE_SCENARIO = {
    "fault": "dependency_outage",
    "target_service": "postgres",
    "outage_seconds": 3,
    "reading_count": 2,
    "recovery_timeout_seconds": 30,
}
Behaviour = Literal["correct", "no-recovery", "duplicate-side-effect"]


class _Response:
    """Stand in for one httpx response."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        """Return the recorded body."""
        return dict(self._body)


class _FakeStack:
    """One in-memory stand-in for the API, Docker Compose, the queue, and the clock.

    ``correct`` completes every queued reading once the dependency is started again and
    treats a replay as the supplied pipeline does: same identity, same terminal record.
    ``no-recovery`` never completes anything. ``duplicate-side-effect`` completes
    everything but re-queues an already-completed identity on replay, which is exactly
    the duplicate a broken idempotency store would produce.
    """

    def __init__(self, behaviour: Behaviour) -> None:
        self.behaviour = behaviour
        self.records: dict[str, dict[str, Any]] = {}
        self.compose_calls: list[tuple[str, ...]] = []
        self.dependency_up = True
        self.dependency_restarted = False
        self._clock: Iterator[float] = (float(tick) for tick in itertools.count())

    # -- seams -------------------------------------------------------------------
    def compose(self, *arguments: str) -> None:
        self.compose_calls.append(arguments)
        if arguments == ("stop", "postgres"):
            self.dependency_up = False
        if arguments == ("start", "postgres"):
            self.dependency_up = True
            self.dependency_restarted = True

    def now(self) -> float:
        return next(self._clock)

    def client(self, **_: Any) -> Self:
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> Literal[False]:
        return False

    # -- the API -----------------------------------------------------------------
    def post(self, path: str, *, json: dict[str, Any]) -> _Response:
        assert path == "/api/v1/readings"
        if not self.dependency_up:
            return _Response(500, {})
        exception_id = f"exc-{json['reading_id']}"
        record = self.records.get(exception_id)
        if record is None:
            record = {"exception_id": exception_id, "state": "QUEUED", "updated_at": 1}
            self.records[exception_id] = record
        elif self.behaviour == "duplicate-side-effect":
            record["state"] = "QUEUED"
            record["updated_at"] += 1
        return _Response(202, {"exception_id": exception_id, "state": record["state"]})

    def get(self, path: str) -> _Response:
        if not self.dependency_up:
            return _Response(500, {})
        exception_id = path.rsplit("/", maxsplit=1)[1]
        record = self.records.get(exception_id)
        if record is None:
            return _Response(404, {})
        if (
            record["state"] == "QUEUED"
            and self.dependency_restarted
            and self.behaviour != "no-recovery"
        ):
            record["state"] = "COMPLETED"
            record["updated_at"] += 1
        return _Response(200, dict(record))


def _install(monkeypatch: pytest.MonkeyPatch, stack: _FakeStack) -> None:
    """Point every seam of the procedure at the stand-in."""
    monkeypatch.setattr(held_out_review, "_compose", stack.compose)
    monkeypatch.setattr(held_out_review, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(held_out_review, "_now", stack.now)
    monkeypatch.setattr(held_out_review, "_sqs", lambda: None)
    monkeypatch.setattr(held_out_review, "_queue_depths", lambda _sqs: (0, 0))
    monkeypatch.setattr(held_out_review, "_redrive_dead_letters", lambda _sqs: 0)
    monkeypatch.setattr(httpx, "Client", stack.client)


def test_held_out_check_mechanism_with_a_fake_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mechanism must stage, inject, wait, and grade a recovering pipeline as a pass."""
    stack = _FakeStack("correct")
    _install(monkeypatch, stack)

    assert run_held_out_check(json.dumps(FAKE_SCENARIO), "http://unused.invalid")
    assert stack.compose_calls[:4] == [
        ("stop", "worker"),
        ("stop", "postgres"),
        ("start", "worker"),
        ("start", "postgres"),
    ]
    assert len(stack.records) == FAKE_SCENARIO["reading_count"]


def test_a_pipeline_that_never_recovers_is_a_failed_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reading that never reaches a terminal state within the timeout fails the grade."""
    stack = _FakeStack("no-recovery")
    _install(monkeypatch, stack)

    assert run_held_out_check(json.dumps(FAKE_SCENARIO), "http://unused.invalid") is False
    # The stack is left as found even on a failed grade.
    assert stack.compose_calls[-1] == ("start", "worker")
    assert stack.dependency_up


def test_a_duplicate_side_effect_is_a_failed_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Completing every reading is not enough if replaying one re-queues it.

    Without this negative, the no-duplicate assertion would be vestigial: a procedure that
    only checked terminal states would pass a pipeline whose idempotency store had been
    broken, which is the one regression this Sprint's reliability work exists to prevent.
    """
    stack = _FakeStack("duplicate-side-effect")
    _install(monkeypatch, stack)

    assert run_held_out_check(json.dumps(FAKE_SCENARIO), "http://unused.invalid") is False


def test_an_unreachable_api_is_unusable_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stack that did not answer is our fault: this job runs the supplied tree."""
    stack = _FakeStack("correct")
    _install(monkeypatch, stack)

    def refuse(path: str, *, json: dict[str, Any]) -> _Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(stack, "post", refuse)

    with pytest.raises(ScenarioError):
        run_held_out_check(json.dumps(FAKE_SCENARIO), "http://unused.invalid")
    # Restoration still ran: the dependency was never stopped, so only the worker restarts.
    assert stack.compose_calls == [("stop", "worker"), ("start", "worker")]


@pytest.mark.parametrize(
    "scenario",
    [
        "",
        "not json",
        "{}",
        json.dumps({key: value for key, value in FAKE_SCENARIO.items() if key != "outage_seconds"}),
        json.dumps({**FAKE_SCENARIO, "surprise": 1}),
        json.dumps({**FAKE_SCENARIO, "outage_seconds": True}),
        json.dumps({**FAKE_SCENARIO, "reading_count": 0}),
        json.dumps({**FAKE_SCENARIO, "recovery_timeout_seconds": 3}),
        json.dumps({**FAKE_SCENARIO, "fault": "consumer_outage"}),
        json.dumps({**FAKE_SCENARIO, "target_service": "worker"}),
    ],
    ids=[
        "empty",
        "not-json",
        "no-keys",
        "missing-key",
        "unexpected-key",
        "boolean-count",
        "zero-readings",
        "timeout-not-past-outage",
        "unknown-fault",
        "unknown-target",
    ],
)
def test_an_unusable_scenario_is_refused_before_anything_is_graded(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """A missing or malformed scenario must be refused, never graded, and touch nothing."""
    stack = _FakeStack("correct")
    _install(monkeypatch, stack)

    with pytest.raises(ScenarioError):
        run_held_out_check(scenario, "http://unused.invalid")
    assert stack.compose_calls == []


def test_the_entry_point_separates_a_failed_grade_from_an_unusable_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0, 1, and 2 must mean pass, failed grade, and unusable run.

    The workflow reads these codes to decide between reporting `success`, `failure`, and
    `error` on the pull request. Collapsing the last two would show a student a wrong
    answer where the fault was a missing secret or a stack that did not start.
    """
    _install(monkeypatch, _FakeStack("correct"))
    assert main(json.dumps(FAKE_SCENARIO), "http://unused.invalid") == 0
    assert capsys.readouterr().out.strip() == PASS

    _install(monkeypatch, _FakeStack("no-recovery"))
    assert main(json.dumps(FAKE_SCENARIO), "http://unused.invalid") == 1
    assert capsys.readouterr().out.strip() == FAIL

    assert main("", "http://unused.invalid") == 2
    unusable = capsys.readouterr()
    assert INFRASTRUCTURE in unusable.err
    assert PASS not in unusable.out and FAIL not in unusable.out


def test_no_scenario_content_reaches_the_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A student reads this output, so nothing about the scenario may appear in it."""
    _install(monkeypatch, _FakeStack("correct"))
    main(json.dumps(FAKE_SCENARIO), "http://unused.invalid")
    captured = capsys.readouterr()
    assert captured.out.strip() == PASS
    assert captured.err == ""
