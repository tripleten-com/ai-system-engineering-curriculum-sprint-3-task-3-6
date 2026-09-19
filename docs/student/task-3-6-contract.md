# Task 3.6 — Failure lab and held-out check contract

Run the supplied failure lab against your own live stack, watch the queue and the exception
records while the system loses and regains its worker, then write down what you saw and how you
knew it was over: one bounded runbook, from detection to verification. Separately, extend the
`JobQueue` fidelity record with the ECS behaviour this local evidence cannot prove. You write two
Markdown files. You do not touch the transport, either adapter, the worker, the queue's own
dead-letter policy from Task 3.3, the alert from Task 3.4, or the CI gate from Task 3.5.

## The one failure lab, and the one held-out scenario

`poe dev-failure-lab` is the development failure scenario. It stops the `worker` container for a
bounded window, submits five fresh readings through the API so a backlog builds on the
`coldline-exception-jobs` queue, prints the queue depth it observes while the consumer is gone,
restarts the worker, and waits for every submitted exception to reach `COMPLETED` with both the
main queue and the dead-letter queue back at zero. Nothing in the supplied pipeline is broken;
the lab shows you what a bounded consumer outage looks like in the queue, in the exception
records, and on the dashboards, and what recovery looks like when it is over.

Be precise about what this fault does and does not do. A stopped consumer never *receives*, and
SQS only redrives a message after `maxReceiveCount` actual receive attempts, so stopping the worker
does **not** dead-letter anything by itself: messages simply accumulate on the main queue with
growing depth and zero in-flight count until the consumer returns. A message that was mid-receive
when the container stopped can, rarely, expire back onto the queue and count one attempt; the lab
tolerates that edge case (it redrives anything that does land in the dead-letter queue before its
final check) but never depends on it.

The held-out scenario is a *different* fault against the same supplied pipeline, graded in a
protected job you do not run and cannot see the parameters of: a bounded dependency outage rather
than a consumer outage. It uses the same fault controls (`docker compose stop`/`start` on one
service) and the same evidence shape (queue depth, dead-letter depth, per-exception terminal
state, an idempotent-replay check) as the lab. It grades the **supplied** pipeline — the
idempotent worker, the SQS visibility timeout and redrive policy you configured in Task 3.3 —
not your runbook. Its procedure is public and readable at `tests/contract/held_out_review.py`;
only its input is protected. A submission that changes the supplied system is refused rather
than graded.

## What is assessed, and by whom

| Assessed | By |
|---|---|
| The pull request changes only `submission.yaml`, `docs/student/runbook.md`, `docs/fidelity/JobQueue.md`, and files you add under `tests/student/` | Automated, in this repository |
| `docs/student/runbook.md` exists and carries the four required sections, each with content | Automated, statically (`poe runbook-contract`) |
| `docs/fidelity/JobQueue.md` keeps Task 3.3's SQS limits and adds the required ECS section with every required limitation code | Automated, statically (part of `poe runbook-contract`) |
| The supplied pipeline recovers every reading exactly once from one isolated held-out failure scenario | Automated, in the protected held-out job, against the supplied base tree |
| Whether your runbook describes a real detection-to-verification pass, and whether your ECS limitations are defensible | Your instructor, at the Task 3.7 Project Defense |

## What is already supplied

| Supplied | Where | Note |
|---|---|---|
| The development failure lab | `tests/failure/dev_failure_lab.py` | run it; do not edit it |
| Task 3.3's own exercise scripts and queue diagnostics | `tests/failure/force_dlq_arrival.py`, `tests/failure/redrive_and_verify.py`, `tests/failure/queue_client.py` | reuse them for queue and dead-letter depth; do not edit them |
| Task 3.4's own exercise scripts | `tests/failure/trigger_alert_load.py`, `tests/failure/verify_alert_recovery.py` | unchanged; not this Task's exercise |
| The held-out procedure | `tests/contract/held_out_review.py` | public; its input is protected and never committed |
| Task 3.3's own settled dead-letter redrive policy | `compose.yaml` | unchanged; not this Task's editable surface |
| Task 3.4's own settled alert window | `infra/observability/alerts.yml` | unchanged; not this Task's editable surface |
| Task 3.5's own settled reliability gate | `.github/workflows/task.yml` | unchanged; `.github/workflows/task.yml` is no longer student-editable |
| Task 3.3's `JobQueue` fidelity record | `docs/fidelity/JobQueue.md` | keep every existing paragraph; add the new section below it |

## The two files

### `docs/student/runbook.md`

Create this file. It does not exist in the starter. It must contain exactly these four level-two
headings, on their own lines, spelled and capitalised exactly like this, in this order, each
exactly once:

```markdown
## Detection
## Diagnosis
## Recovery
## Verification
```

Each section must contain at least one line of your own text that is not a heading. What goes in
them is yours to write, from your own `poe dev-failure-lab` run and what you observed alongside it:

- **Detection** — how you knew something was wrong before the lab told you: the queue-depth
  signal, what the Grafana dashboard and Prometheus showed while the worker was gone, whether the
  Task 3.4 alert fired (and why it should not have, since nothing was dead-lettered).
- **Diagnosis** — how you told a consumer outage apart from a dependency outage or a poison
  message: growing depth with zero in-flight and zero dead-letter depth points at an absent
  consumer, not at a failing one.
- **Recovery** — the bounded action that restored service, why it was safe to take without
  duplicating work (the idempotent `ExceptionRecord` state machine and the queue's own redrive
  policy), and what you would have done if messages *had* reached the dead-letter queue.
- **Verification** — how you knew it was over: every submitted exception at `COMPLETED`, both
  queue depths at zero, the evidence JSON `poe dev-failure-lab` printed.

`poe runbook-contract` checks only that the file exists, that the four headings are present as
specified, and that each section has content. It does not grade what you wrote. Reasoning quality
is for the Task 3.7 Project Defense.

### `docs/fidelity/JobQueue.md`

Edit this file in place. Task 3.3 already records what LocalStack SQS does not prove about managed
SQS; keep every existing paragraph. Add one new section at the end, with exactly this heading:

```markdown
## ECS fidelity limits (Task 3.6)
```

Inside it, state what stopping and starting one Compose `worker` container proves nothing about
when the same worker is an Amazon ECS service. Each limitation is one line beginning with its
code, a colon, and your explanation. The section must carry all four of these codes:

| Code | Covers |
|---|---|
| `ECS-01` | Task placement: one container on one host says nothing about how a cluster places, packs, or constrains tasks across instances or capacity providers |
| `ECS-02` | Service-scheduler restart: `docker compose start worker` is an operator action; an ECS service scheduler replaces a stopped or failed task automatically to hold its desired count, with its own latency and backoff you never observed here |
| `ECS-03` | Autoscaling: the backlog you built never triggered scale-out; ECS Service Auto Scaling on queue depth, and how many concurrent consumers would drain it, is unproven |
| `ECS-04` | Health-check grace and load-balancer behaviour: Compose's healthcheck and `up --wait` are not an ECS health-check grace period, ALB target-group health checks, or deregistration draining |

For example, a valid line is:

```markdown
- ECS-01: Compose ran one worker on one host; no placement strategy, bin-packing, or capacity provider was involved, so nothing here proves a replacement task could be placed.
```

`poe runbook-contract` (the same one command that checks the runbook) also checks that Task 3.3's
existing statement of SQS limits is still present, that the new heading exists, and that each
required code appears in that section on a line of the form above (optionally as a list bullet,
optionally bold), followed by a colon and text. It does not grade the depth of your reasoning. You
may add further codes (`ECS-05`, …) if you have more to say. A narrower `poe fidelity-check` alias
runs only this half, if you want to isolate it while iterating.

## The exercise

Run this against the live stack, after `poe start`:

```shell
poe dev-failure-lab   # stops the worker, builds a backlog, restarts it, waits for full recovery
```

It prints progress as it goes and one JSON evidence blob at the end: the five exception ids, the
main-queue and dead-letter depths observed while the worker was stopped, how many messages (if any)
it had to redrive, every exception's terminal state, both final depths, and the recovery time. Exit
code `0` means every reading reached `COMPLETED` and both depths are zero. Keep the blob; your
runbook's Verification section should be able to point at it.

While it runs, watch the queue from the other side: the Grafana diagnostics dashboard, Prometheus's
`coldline_job_queue_depth` and `coldline_job_queue_dead_letter_depth` (remember the depth gauge is
reported by the worker itself, so it goes quiet while the worker is stopped — that silence is a
signal too), and the Alertmanager API. `poe inject-failure` and `poe redrive` from Task 3.3 remain
available if you want to contrast a real dead-letter arrival with this lab's backlog.

## Commands

```shell
poe dev-failure-lab    # the development failure scenario, against your live stack
poe runbook-contract   # the one public check: docs/student/runbook.md's sections and
                        # docs/fidelity/JobQueue.md's ECS section, together
poe fidelity-check     # optional: just the fidelity-record half, for isolating while iterating
poe verify             # the full public student verification path
```

## What the checks verify

| Check | What it looks at |
|---|---|
| `test_runbook_has_the_required_sections` | `docs/student/runbook.md` exists; `## Detection`, `## Diagnosis`, `## Recovery`, and `## Verification` each appear exactly once, in that order, as whole lines; every section contains at least one non-heading, non-empty line |
| `test_fidelity_record_states_the_ecs_limits` | `docs/fidelity/JobQueue.md` still contains Task 3.3's sentence beginning "This local implementation does not claim IAM enforcement"; the `## ECS fidelity limits (Task 3.6)` heading exists; `ECS-01`, `ECS-02`, `ECS-03`, and `ECS-04` each appear inside that section at the start of a line, followed by a colon and text |
| `tests/contract/held_out_review.py` (protected job) | The supplied pipeline, on the base tree, recovers every reading of one isolated dependency-outage scenario to `COMPLETED` within the scenario's timeout, replaying each reading changes nothing, and both queue depths return to zero |
| `test_held_out_check_mechanism_with_a_fake_scenario` and its siblings | The held-out procedure's mechanism, against a fake, non-secret scenario with Docker, the queue, and HTTP stubbed: pass, failed grade, and unusable run are classified apart, and nothing about a scenario reaches the output |

## Student-editable paths

- `docs/student/runbook.md` (new)
- `docs/fidelity/JobQueue.md` (edit in place)
- `tests/student/` (additions only)
- `submission.yaml`

Keep the worker, both adapters, the failure-lab and exercise scripts, `compose.yaml`,
`infra/observability/alerts.yml`, `.github/workflows/task.yml`, and every test file exactly as
supplied. The public checks compare them, and the held-out job refuses to grade a submission that
changed the supplied system. Recovering the supplied pipeline is not this Task's assignment — it
already recovers; observing that recovery, writing it down as a bounded runbook, and stating
honestly what the local evidence cannot prove about ECS, is.
