# Coldline Task 3.6 — Failure lab and held-out check

This checkpoint runs the Sprint's one development failure lab and writes the one bounded recovery
runbook. The supplied pipeline is complete and correct: the idempotent worker from Task 3.2, the SQS
transport and dead-letter policy from Task 3.3, the alert from Task 3.4, and the CI gate from Task
3.5 all ship settled. What is not yet written is your account of a failure: `poe dev-failure-lab`
takes the worker away for a bounded window, builds a queue backlog, brings the worker back, and
waits for full recovery; you watch it, then write `docs/student/runbook.md` (detection, diagnosis,
recovery, verification) and extend `docs/fidelity/JobQueue.md` with what this local evidence cannot
prove about the same worker running as an Amazon ECS service. A separate, isolated held-out failure
scenario — a different fault against the same supplied pipeline — is graded in a protected job
whose parameters you never see.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/tripleten-com/ai-system-engineering-curriculum-sprint-3-task-3-6/tree/main)

## Start the system

Prerequisites are Python 3.12 and Docker with Compose v2. The supplied bootstrap supports macOS
arm64/x86-64, Windows x86-64, and Linux x86-64/aarch64, and installs pinned uv 0.11.8 under
`.tools/bin`. If your computer cannot run the stack locally, use the Codespaces button above.

On macOS and most Linux distributions the interpreter is `python3`; substitute it wherever these
commands say `python`.

```shell
python infra/scripts/bootstrap.py
./.tools/bin/uv sync --frozen
./.tools/bin/uv run --frozen poe preflight
./.tools/bin/uv run --frozen poe start
./.tools/bin/uv run --frozen poe ready
```

PowerShell and POSIX wrappers are available under `infra/scripts/`. After uv is on `PATH`, the
shorter `uv run --frozen poe <task>` form works.

| Service | Local URL | Purpose |
|---|---|---|
| API | `http://localhost:8000` | Submit exception workflows and retrieval queries; `/version` names the build that answers |
| Grafana | `http://localhost:3000` | Use the focused diagnostics dashboard |
| Prometheus | `http://localhost:9090` | Query bounded metrics and inspect the deployed alert rule |
| Alertmanager | `http://localhost:9093` | Inspect firing and resolved alerts |
| Jaeger | `http://localhost:16686` | Inspect local traces |
| LocalStack S3/SQS | `http://localhost:4566` | Inspect the emulated object-storage and queue endpoint |

Each of these ports can be overridden by setting the matching `COLDLINE_API_HOST_PORT`,
`COLDLINE_GRAFANA_HOST_PORT`, `COLDLINE_PROMETHEUS_HOST_PORT`, `COLDLINE_ALERTMANAGER_HOST_PORT`,
`COLDLINE_JAEGER_HOST_PORT`, or `COLDLINE_LOCALSTACK_HOST_PORT` environment variable in your shell
environment or a local `.env` file (copy `.env.example`) if a default collides with something
already running on your machine. Keep the override in place for every `poe` command.

PostgreSQL, Redis, worker metrics, and OTLP remain inside the Compose network. Codespaces uses the
same `compose.yaml` and keeps every forwarded port private. Redis keeps running in this Task only
for an earlier checkpoint's own contract test; no composition root reads it anymore.

## Command path

For this Task, run the supplied commands in this order:

```text
poe start
poe dev-failure-lab
poe runbook-contract
poe verify
```

| Command | Use |
|---|---|
| `poe dev-failure-lab` | Stop the worker for a bounded window, build a queue backlog, restart it, and wait for every reading to recover |
| `poe runbook-contract` | The one public check: `docs/student/runbook.md` has its four required sections, and `docs/fidelity/JobQueue.md` carries the ECS fidelity section and every required code |
| `poe fidelity-check` | Optional: just the fidelity-record half of `poe runbook-contract`, for isolating while iterating |
| `poe held-out-dry-run` | Exercise the held-out procedure's mechanism against a fake, non-secret scenario, with no Docker |
| `poe queue-contract` | Run Task 3.3's own automated dead-letter redrive check |
| `poe slo-contract` | Run Task 3.4's own automated alert-bound, firing, and resolution checks |
| `poe gate-contract` | Run Task 3.5's own automated static-wiring and live-rejection checks |
| `poe contract` | Check interfaces, boundaries, submissions, and repository structure |
| `poe smoke` | Check the initialized running platform |
| `poe e2e` | Run the external API-to-worker workflow |
| `poe verify` | Run the public student verification path |
| `poe student-tests` | Run your own tests under `tests/student/` |
| `poe restart` | Restart the existing API and worker containers **without rebuilding** |
| `poe stop` | Remove containers and the network, keeping named volumes |
| `poe reset` | Remove containers, the network, and local named volumes |

For Task 3.6, `poe verify` starts the stack, ingests the supplied corpus, runs the smoke tests,
the end-to-end exception workflow, the queue-contract, SLO-contract, and gate-contract checks, this
Task's own runbook and fidelity checks, the held-out dry run, and the answer-sheet checks. Run the
failure lab by hand, against the live stack, as described in the Task contract — it stops and
restarts a container, so it is not part of `poe verify` and never runs in automated CI.

## Folder map

```text
repository root/
├── docs/                Student guidance, public contracts, and fidelity notes
│   ├── contracts/       Machine-readable public contracts
│   ├── fidelity/        Local-runtime boundary notes, including the JobQueue record you extend
│   ├── architecture/    Supplied vector engine technical profiles, in prose
│   ├── retrieval/       Supplied retrieval pipeline reference
│   └── student/         This Task's contract, and the runbook you write
├── config/              Retrieval configuration, settled and supplied from Sprint 2
├── infra/               Local setup and runtime configuration
│   ├── containers/      The API and worker Dockerfiles, with the build identity arguments
│   ├── observability/   Prometheus, Alertmanager, and Grafana configuration
│   ├── release/         The supplied Task 3.1 release manifest, unchanged
│   ├── corpus/          Supplied synthetic corpus, query set, and designated investigation
│   ├── judge/            Supplied cached judge evidence and its provenance record
│   ├── profiles/        Supplied engine and emulator profiles, and their provenance record
│   └── postgres/        Database initialization and the migration baseline stamp
├── loadtest/            Supplied traffic profile and provider-latency harness
├── migrations/          Alembic environment, revision template, and revisions
├── src/
│   ├── api/             HTTP application code, the retrieval and document paths, composition
│   ├── worker/          Background application code, including the dead-letter depth poller
│   ├── domain/          Shared domain code, contracts, the failure taxonomy, service and repository contracts
│   ├── ports/           Application interfaces
│   └── adapters/        Technology-specific implementations, including the supplied SQS/DLQ queue adapter
└── tests/
    ├── unit/            Isolated behavior checks
    ├── benchmark/       Supplied evaluation harness, metrics, and adoption policy
    ├── contract/        Interface, retrieval, and repository checks, and the public held-out procedure
    ├── diagnostics/     Supplied stage inspector
    ├── doubles/         Supplied deterministic test doubles
    ├── failure/         Supplied failure-lab and exercise scripts — run them, do not edit them
    ├── student/         Your own tests
    ├── smoke/           Running-platform checks
    └── e2e/             Supplied workflow tools and checks
```

## Overview

Use the Task 6 lesson (Task 3.6 in this repository) to decide what to do. This README covers
local setup and repository orientation.

1. `README.md` — local setup, commands, and permitted changes.
2. [`docs/student/task-3-6-contract.md`](docs/student/task-3-6-contract.md) — the failure lab, the
   two files you write and their exact required structure, what each check verifies, and the
   permitted paths.
3. `tests/failure/dev_failure_lab.py` — the failure lab you run against the live stack; read its
   docstrings to see exactly what it does and what it does not do.
4. [`docs/fidelity/JobQueue.md`](docs/fidelity/JobQueue.md) — Task 3.3's record of what LocalStack
   SQS does not prove, which you extend with the ECS section.
5. `tests/contract/held_out_review.py` — the public procedure the protected held-out job runs; its
   input is protected and never committed.

The application source lives in five flat packages:

| Package | Responsibility |
|---|---|
| `api` | HTTP delivery, API use cases, the retrieval workflow, versioned routes, configuration, and composition |
| `worker` | Background processing, retries, the dead-letter depth poller, configuration, and composition |
| `domain` | Provider-neutral contracts, state rules, identity, redaction, embedding, chunking, fusion, access constraints, failure classification, service and repository contracts |
| `ports` | Exactly five visible application interfaces |
| `adapters` | PostgreSQL, pgvector retrieval, LocalStack SQS/DLQ, S3-compatible object storage, deterministic model, the resilient model-provider wrapper, logs, traces |

`src/api/bootstrap.py` and `src/worker/bootstrap.py` compose each process from its settings and
adapters. Process settings live in `src/api/config.py` and `src/worker/config.py`.

## The five ports

Find the available interfaces in `src/ports/`. A port describes an application capability; an
adapter provides it using a concrete technology.

| Port | General responsibility |
|---|---|
| `ModelProvider` | Call an AI model service |
| `Retriever` | Look up relevant context or documents |
| `ObjectStore` | Store large binary objects or files |
| `JobQueue` | Publish and consume background work |
| `SecretProvider` | Read API keys and credentials |

LocalStack SQS, with a bound dead-letter queue, still carries `JobQueue`, unchanged from Task 3.3.
The dead-letter depth poller reads the dead-letter queue's own attribute directly, alongside
`JobQueue` rather than through it; see [JobQueue fidelity](docs/fidelity/JobQueue.md).

## Test levels

| Level | Requires Compose | Main question |
|---|---:|---|
| Unit | No | Does one responsibility behave correctly, including failures? |
| Contract | Some | Do interfaces, schemas, paths, and dependency rules stay compatible? |
| Smoke | Yes | Did the complete local platform initialize and become observable? |
| E2E | Yes | Can an external client complete the supplied workflow? |

Contract checks marked `runtime` need the running stack. `poe contract` skips them; `poe verify`,
`poe runtime-contract`, `poe queue-contract`, `poe slo-contract`, and `poe gate-contract` run them.
This Task's own check, `poe runbook-contract`, is static and needs no stack; it is marked
`assessed`, so a fresh starter is expected to fail it until you have written the two files.

## Submission checks

Run `poe verify` locally before opening your student pull request. Public GitHub CI repeats
the student checks. This Task records `answers: {}`: your runbook, your fidelity record, and the
observed recovery are the evidence, so there is no separate protected answer check. The protected
held-out job additionally grades the supplied pipeline against one isolated failure scenario and
reports `success`, `failure`, or `error` on your pull request's exact commit; it never prints the
scenario. Follow the Task lesson's instructor-review and progression policy.

## Task boundary

Task 3.6 asks you to run `poe dev-failure-lab` against your live stack, write
`docs/student/runbook.md` with its four required sections, and add the required ECS fidelity
section to `docs/fidelity/JobQueue.md`.

These paths are student-editable:

- `docs/student/runbook.md` (new)
- `docs/fidelity/JobQueue.md` (edit in place)
- `tests/student/` (additions only)
- `submission.yaml`

Keep the worker, both adapters, the failure-lab and exercise scripts, Task 3.3's own settled
`compose.yaml`, Task 3.4's own settled `infra/observability/alerts.yml`, Task 3.5's own settled
`.github/workflows/task.yml`, and every test file exactly as supplied; the public checks compare
them, and the held-out job refuses to grade a submission that changed the supplied system.
Everything else in this repository is supplied.

### Student walkthrough

See **Task 6: Failure lab and runbook** in your course platform for the full walkthrough. In
outline: read `docs/student/task-3-6-contract.md`, run `poe dev-failure-lab` against the live
stack while watching the queue, the dashboard, and the exception records, write
`docs/student/runbook.md` with its four required sections, add the `## ECS fidelity limits
(Task 3.6)` section with its four required codes to `docs/fidelity/JobQueue.md`, run
`poe runbook-contract` until it passes, run `poe verify`, and open your pull request.

## Operational limits

This local system does not authenticate users, terminate TLS, or manage production secrets.
The Compose PostgreSQL password and the LocalStack access keys are local-only non-secret
credentials. Never place real credentials, personal data, or production records in this
repository.

Alertmanager here is configured with a "default" receiver that has no notification integration:
alerts are queryable through its own API but never sent anywhere real. Never add a webhook, email,
Slack, or paid integration; Sprints 1-4 are emulator-only and never call a hosted endpoint.

LocalStack's SQS emulation is a local reliability primitive, not a managed-service durability,
IAM, availability, or cost claim. Stopping and starting one Compose container is a local fault
control, not an ECS service event. See [JobQueue fidelity](docs/fidelity/JobQueue.md) for the
exact boundary, and extend it as this Task asks.

Named volumes preserve local PostgreSQL, Redis, Prometheus, Alertmanager, Grafana, and Jaeger state
across `poe stop`. LocalStack object and queue contents are deliberately not persisted; the
initializer re-uploads the supplied corpus artifacts and re-provisions the queue on every start.
The `poe reset` command deletes the named volumes. This topology makes no backup, replication,
high-availability, disaster-recovery, capacity, latency-SLO, or availability claim beyond the one
alert Task 3.4 configures, the one CI gate Task 3.5 wires to it, and the one bounded recovery this
Task's failure lab demonstrates.

See [JobQueue fidelity](docs/fidelity/JobQueue.md),
[ModelProvider fidelity](docs/fidelity/ModelProvider.md),
[ObjectStore fidelity](docs/fidelity/ObjectStore.md), and
[Retriever fidelity](docs/fidelity/Retriever.md) for the active adapter boundaries. The
[local runtime evidence](docs/fidelity/local-runtime.md) records the current measurement and its
qualification limits.
