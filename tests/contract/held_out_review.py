"""Coldline.

===================

File:              tests/contract/held_out_review.py
Component:         Contract — Held-out review
Purpose:           Run one held-out dependency-outage scenario and grade it without exposing it.
Interacts With:    The live API, LocalStack SQS, Docker Compose, a runtime-supplied scenario
Sprint/Task:       Sprint 3 — Project 3 / Task 3.6
Concepts:          Held-out evaluation, protected grading, idempotent recovery
Tools:             Python 3.12, httpx, boto3, Docker Compose

Sprint 3's only held-out scenario runs here, in protected CI, after the public checks.
The scenario arrives at runtime and is never committed: this file contains the
*procedure*, not the parameters.

The fault is a bounded outage of one dependency the supplied pipeline needs, injected with
the same fault controls the development failure lab uses (``docker compose stop`` and
``start`` on one service) and reported in the same evidence shape (per-exception terminal
state, main-queue and dead-letter depth, an idempotent-replay check). The procedure stops
the worker so a known set of fresh readings can be queued, stops the target dependency,
starts the worker into the outage, waits the scenario's outage window, starts the
dependency again, and then requires every reading to reach ``COMPLETED`` within the
scenario's recovery timeout, replaying each reading to change nothing, and both queue
depths to return to zero. It grades the SUPPLIED pipeline - the idempotent
``ExceptionRecord`` state machine, the SQS visibility timeout, and the redrive policy
settled in Task 3.3 - never student code.

Nothing about the scenario is printed. A student reading CI output learns whether the
pipeline passed, and nothing about how long the outage was or how many readings it held.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from adapters.queue import create_sqs_client

TASK_ROOT = Path(__file__).resolve().parents[2]
PASS = "HELD_OUT_CHECK_PASSED"
FAIL = "HELD_OUT_CHECK_FAILED"
# Printed instead of any exception text. A traceback here could carry the scenario's
# parameters, a Compose error naming what was stopped, or an API response into a log a
# student can read.
INFRASTRUCTURE = "HELD_OUT_INFRASTRUCTURE_ERROR: contact course support."
FAULTS = ("dependency_outage",)
TARGET_SERVICES = ("postgres",)
REQUIRED_KEYS = frozenset(
    {"fault", "target_service", "outage_seconds", "reading_count", "recovery_timeout_seconds"}
)
# The same queue names Task 3.3's exercise scripts use. Duplicated rather than imported:
# this file runs as `python tests/contract/held_out_review.py` from the base tree, where
# the `tests` package is not on the import path, exactly like Task 2.8's procedure.
QUEUE_NAME = "coldline-exception-jobs"
DEAD_LETTER_NAME = "coldline-exception-jobs-dlq"
POLL_INTERVAL_SECONDS = 2.0
REPLAY_ATTEMPTS = 5
TERMINAL_STATES = {"COMPLETED", "FAILED"}


class ScenarioError(Exception):
    """Report that the supplied scenario is unusable, without describing it.

    Distinct from a failed grade on purpose. A malformed scenario, an unreachable API, a
    Compose command that did not run, or a stack that never answered again is an
    infrastructure fault; reporting it as "did not pass" would blame the submission for
    a problem on this side. The message carries no detail for the same reason the grade
    carries none.
    """


def _validated_scenario(scenario_json: str) -> dict[str, Any]:
    """Return the scenario's parameters, or refuse to grade.

    The shape is checked strictly and up front. A scenario with an unexpected key, a
    missing field, a value outside its bound, or a recovery timeout that cannot outlast
    its own outage cannot produce a meaningful grade, and silently treating it as a
    failure would record a wrong result rather than an unusable one.
    """
    try:
        scenario = json.loads(scenario_json)
    except ValueError as exc:
        raise ScenarioError("Invalid held-out configuration") from exc
    if not isinstance(scenario, dict) or set(scenario) != REQUIRED_KEYS:
        raise ScenarioError("Invalid held-out configuration")
    if scenario["fault"] not in FAULTS or scenario["target_service"] not in TARGET_SERVICES:
        raise ScenarioError("Invalid held-out configuration")
    for key, low, high in (
        ("outage_seconds", 1, 300),
        ("reading_count", 1, 20),
        ("recovery_timeout_seconds", 1, 900),
    ):
        value = scenario[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ScenarioError("Invalid held-out configuration")
    if scenario["recovery_timeout_seconds"] <= scenario["outage_seconds"]:
        raise ScenarioError("Invalid held-out configuration")
    return scenario


def _compose(*arguments: str) -> None:
    """Run one Docker Compose command from the Task root, keeping its output private."""
    subprocess.run(
        ["docker", "compose", *arguments],
        cwd=TASK_ROOT,
        check=True,
        capture_output=True,
    )


def _sleep(seconds: float) -> None:
    """Wait; a module-level seam so a dry run can replace the clock."""
    time.sleep(seconds)


def _now() -> float:
    """Read the monotonic clock; a module-level seam so a dry run can replace it."""
    return time.monotonic()


def _sqs() -> Any:
    """Return one SQS client reaching LocalStack from the host, not a container."""
    port = os.environ.get("COLDLINE_LOCALSTACK_HOST_PORT", "4566")
    return create_sqs_client(
        endpoint_url=f"http://localhost:{port}",
        region_name="us-east-1",
        access_key_id="localstack-development-key",
        secret_access_key="localstack-development-secret",
    )


def _queue_depths(sqs: Any) -> tuple[int, int]:
    """Return the approximate main-queue and dead-letter-queue depths."""
    depths = []
    for name in (QUEUE_NAME, DEAD_LETTER_NAME):
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
        attributes = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]
        depths.append(int(attributes["ApproximateNumberOfMessages"]))
    return depths[0], depths[1]


def _redrive_dead_letters(sqs: Any) -> int:
    """Move every dead-lettered message back to the main queue; return how many moved.

    The same receive/send/delete shape as Task 3.3's ``redrive_and_verify.py``. A
    dead-letter arrival is a tolerated recovery path here, not the expected one: the
    scenario's outage is shorter than the queue's visibility timeout, so a message first
    received during the outage is redelivered only after the dependency is back.
    """
    main_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    dlq_url = sqs.get_queue_url(QueueName=DEAD_LETTER_NAME)["QueueUrl"]
    moved = 0
    while True:
        response = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
        messages = response.get("Messages") or []
        if not messages:
            return moved
        for message in messages:
            sqs.send_message(QueueUrl=main_url, MessageBody=message["Body"])
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=message["ReceiptHandle"])
            moved += 1


def _build_reading() -> dict[str, Any]:
    """Build one reading with a fresh identity, so runs never collide on idempotent replay."""
    token = uuid.uuid4().hex[:12]
    return {
        "reading_id": f"reading-held-out-{token}",
        "shipment_id": f"shipment-held-out-{token}",
        "temperature_c": 11.4,
        "allowed_min_c": 2.0,
        "allowed_max_c": 8.0,
        "recorded_at": "2026-01-01T00:00:00Z",
        "context": "Sprint 3 Task 3.6 held-out review",
    }


def _record(client: httpx.Client, exception_id: str) -> dict[str, Any] | None:
    """Return one exception's durable record, or None while the API cannot answer.

    A connection error or a non-200 answer during recovery is expected for a while: the
    API's own pool has to notice its connections died with the dependency. The caller
    keeps polling until the scenario's deadline and decides then.
    """
    try:
        response = client.get(f"/api/v1/exceptions/{exception_id}")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    record = response.json()
    if not isinstance(record, dict):
        return None
    return record


def _replay(client: httpx.Client, reading: dict[str, Any]) -> httpx.Response:
    """Re-submit one reading, retrying briefly through the API's own reconnection."""
    for attempt in range(REPLAY_ATTEMPTS):
        try:
            response = client.post("/api/v1/readings", json=reading)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code < 500:
            return response
        if attempt + 1 < REPLAY_ATTEMPTS:
            _sleep(POLL_INTERVAL_SECONDS)
    raise ScenarioError("Invalid held-out configuration")


def run_held_out_check(scenario_json: str, api_base_url: str) -> bool:
    """Inject the outage, wait for recovery, and grade the outcome.

    Returns True or False for a grade, and raises ``ScenarioError`` when it could not
    grade at all. Neither carries anything about the scenario, so a student reading CI
    output cannot recover its parameters.
    """
    scenario = _validated_scenario(scenario_json)
    target = str(scenario["target_service"])
    outage_seconds = float(scenario["outage_seconds"])
    recovery_timeout = float(scenario["recovery_timeout_seconds"])
    readings = [_build_reading() for _ in range(int(scenario["reading_count"]))]
    dependency_stopped = False
    dependency_restored = False

    try:
        with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
            # Stage the backlog: with the consumer away, every reading is durably QUEUED
            # and its message is on the main queue before the dependency goes down.
            _compose("stop", "worker")
            exception_ids: list[str] = []
            for reading in readings:
                response = client.post("/api/v1/readings", json=reading)
                if response.status_code != 202:
                    raise ScenarioError("Invalid held-out configuration")
                exception_ids.append(str(response.json()["exception_id"]))

            # The fault: the dependency goes away, and the consumer comes back into the
            # outage. Whether the worker's startup fails and Docker restarts it, or it
            # starts and its first receives fail against the missing dependency, the
            # supplied pipeline must recover on its own once the dependency returns.
            _compose("stop", target)
            dependency_stopped = True
            _compose("start", "worker")
            _sleep(outage_seconds)
            _compose("start", target)
            dependency_restored = True

            sqs = _sqs()
            deadline = _now() + recovery_timeout
            answered = False
            terminal: dict[str, str] = {}
            while _now() < deadline:
                for exception_id in exception_ids:
                    if exception_id in terminal:
                        continue
                    record = _record(client, exception_id)
                    if record is None:
                        continue
                    answered = True
                    state = str(record.get("state"))
                    if state in TERMINAL_STATES:
                        terminal[exception_id] = state
                if len(terminal) == len(exception_ids):
                    break
                _, dead_letter_depth = _queue_depths(sqs)
                if dead_letter_depth > 0:
                    _redrive_dead_letters(sqs)
                _sleep(POLL_INTERVAL_SECONDS)
            if len(terminal) < len(exception_ids):
                if not answered:
                    # The API never answered again after the dependency returned. This
                    # job runs the base tree, so that is a problem on this side.
                    raise ScenarioError("Invalid held-out configuration")
                return False
            if any(state != "COMPLETED" for state in terminal.values()):
                return False

            # Acknowledgement follows terminal persistence, so the queue may lag the
            # records by a moment; give it until the same deadline to drain.
            while _now() < deadline:
                queue_depth, dead_letter_depth = _queue_depths(sqs)
                if queue_depth == 0 and dead_letter_depth == 0:
                    break
                _sleep(POLL_INTERVAL_SECONDS)

            # No duplicate side effect: replaying the identical reading must return the
            # same, already-terminal identity and change nothing about its record. That
            # is exactly the guarantee ReadingApplication.accept makes for an identity
            # that is no longer RECEIVED, and what a duplicate consumer would break.
            for reading, exception_id in zip(readings, exception_ids, strict=True):
                before = _record(client, exception_id)
                if before is None:
                    raise ScenarioError("Invalid held-out configuration")
                replay = _replay(client, reading)
                if replay.status_code != 202:
                    return False
                body = replay.json()
                if str(body.get("exception_id")) != exception_id:
                    return False
                if str(body.get("state")) != "COMPLETED":
                    return False
                after = _record(client, exception_id)
                if after != before:
                    return False

            queue_depth, dead_letter_depth = _queue_depths(sqs)
            if queue_depth != 0 or dead_letter_depth != 0:
                return False
    except httpx.HTTPError as exc:
        # The supplied stack did not answer at a point where it had to. This job runs
        # the base tree, so that is a problem on this side rather than a failed grade.
        raise ScenarioError("Invalid held-out configuration") from exc
    except subprocess.SubprocessError as exc:
        raise ScenarioError("Invalid held-out configuration") from exc
    except (KeyError, TypeError, ValueError) as exc:
        # The scenario passed validation, so a shape error this late means the API
        # answered in a form the procedure does not understand.
        raise ScenarioError("Invalid held-out configuration") from exc
    finally:
        # Leave the stack as found on every exit path: whichever step failed, the consumer
        # comes back, and so does the dependency if it was stopped and not yet started.
        # Errors here are swallowed because the grade (or the infrastructure error) has
        # already been decided above.
        services = (
            (target, "worker") if dependency_stopped and not dependency_restored else ("worker",)
        )
        for service in services:
            try:
                _compose("start", service)
            except Exception:  # noqa: BLE001 - best-effort restoration must not mask the grade
                pass
    return True


def main(scenario_json: str, api_base_url: str) -> int:
    """Return 0 for a pass, 1 for a failed grade, and 2 for an unusable run.

    Three exit codes rather than two, so the workflow can report `error` instead of
    `failure` when the fault is on this side. A missing secret, a malformed scenario, or
    a stack that did not start must never look like a wrong answer.
    """
    try:
        passed = run_held_out_check(scenario_json, api_base_url)
    except Exception:
        # Never print the exception: its text may carry protected inputs or protected
        # responses.
        print(INFRASTRUCTURE, file=sys.stderr)
        return 2
    print(PASS if passed else FAIL)
    return 0 if passed else 1


if __name__ == "__main__":
    # The scenario arrives through the environment, never as an argument: a
    # command-line argument is readable from /proc/<pid>/cmdline by any other
    # process for as long as this one lives.
    scenario_env = os.environ.get("HELD_OUT_SCENARIO", "")
    api_base_url_env = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    sys.exit(main(scenario_env, api_base_url_env))
