# Runbook — bounded consumer outage on the exception pipeline

Scope: the `worker` consumer of `coldline-exception-jobs` stops consuming while the API keeps
accepting readings. Written from one `poe dev-failure-lab` run against my own live stack on
2026-09-28, watched from a second terminal that polled SQS, Prometheus, and Alertmanager every
few seconds. Times below are UTC, from that run.

## Detection

The first signal is the **waiting backlog on the main queue**, read from SQS itself, not from the
worker. In my run the second terminal showed:

| Sample (UTC) | `coldline-exception-jobs` visible | in-flight | dead-letter visible | `up{job="coldline-worker"}` |
|---|---:|---:|---:|---|
| 09:42:22 | 0 | 0 | 0 | 1 |
| 09:42:32 | 0 | 0 | 0 | 1 (worker gauges already had no fresh series) |
| 09:42:42 | 5 | 0 | 0 | 0 |
| 09:42:52 | 0 | 0 | 0 | 1 |

What each source did:

- **SQS attributes (host-side, independent of the worker).** `ApproximateNumberOfMessages` on
  `coldline-exception-jobs` rose from 0 to 5 and `ApproximateNumberOfMessagesNotVisible` stayed at
  0. This is the reliable detection signal, because it is read from the queue, not from the
  process that is missing.
- **Prometheus.** `up{job="coldline-worker"}` went to `0` at 09:42:42: the scrape target was gone.
  This is the signal that names *which* component is absent.
- **The worker's own gauges.** `coldline_job_queue_pending_messages` and
  `coldline_job_queue_dead_letter_depth` are exported by the worker, so Prometheus returned no
  series for them while the worker was stopped (empty result at 09:42:32 and 09:42:42). A missing
  sample is not a measurement of zero. In my run the gauge gap started one sample *before*
  `up` flipped, because scrape timing and dependency startup do not line up with the script's
  ten-second pause.
- **Grafana.** The diagnostics dashboard's queue panel plots
  `coldline_job_queue_pending_messages`, i.e. in-flight messages only. It showed a flat line and
  then a gap — it never showed the five waiting messages. Do not use that panel alone to decide
  whether a backlog exists.
- **Alertmanager.** `GET /api/v2/alerts` returned `[]` at every sample.
  `ColdlineDeadLetterQueueBacklog` (`coldline_job_queue_dead_letter_depth > 0 for 15s`) did **not**
  fire, and it was right not to: nothing was dead-lettered. A stopped consumer never *receives*,
  and SQS only redrives after `maxReceiveCount` real receive attempts, so a consumer outage
  produces backlog, not dead letters. **Absence of a page is not absence of an incident here.**

## Diagnosis

Use the three queue numbers together; they separate the three plausible causes without guessing.

| Observation | Absent consumer | Failing consumer / dependency outage | Poison message |
|---|---|---|---|
| visible depth | grows | grows or flat | flat or small |
| in-flight (`NotVisible`) | **0** | **> 0** (messages are being received) | > 0, repeatedly |
| dead-letter depth | **0** | grows once the receive budget is exhausted | grows |
| `up{job="coldline-worker"}` | **0** | 1 | 1 |

My run matched the first column exactly: depth 5, in-flight 0, dead-letter 0, `up` = 0. That is an
**absent consumer**, not a failing one. Nobody was receiving, so no delivery attempt was being
counted and nothing could reach the dead-letter queue.

Confirming steps I used, and would use again:

1. `docker compose --profile observability --profile localstack ps worker` — is the container
   running at all?
2. `docker compose --profile observability --profile localstack logs --tail 50 worker` — did it
   exit, crash-loop, or is it stuck on a dependency?
3. SQS attributes for both queues (the table above) — backlog versus in-flight versus dead-letter.
4. `GET /api/v1/exceptions/{exception_id}` for a few readings accepted during the window — a
   non-terminal state there confirms the readings were accepted and durably queued and that only
   the processing side is missing. The API kept accepting readings and returning an
   `exception_id` throughout my run, so no reading was lost at ingest.

If in-flight had been non-zero with a rising dead-letter depth, the diagnosis would instead be a
consumer that *is* receiving and failing — a dependency outage or a poison message — and the
recovery below would not be enough on its own.

## Recovery

**What the lab did.** It restored the consumer and nothing else:

```
docker compose start worker
```

(`_compose("start", "worker")` in `tests/failure/dev_failure_lab.py`, run from the repository
root). It then polled each exception and both queue depths until every reading was terminal and
both depths were zero. It also carries a redrive fallback — it moves anything found in the
dead-letter queue back to the main queue before its final check — but in my run
`redriven_messages` was `0`, so that path was never taken.

**Why restarting is safe and does not duplicate work.** Two independent properties:

- The `ExceptionRecord` state machine is idempotent: replaying the same `reading_id` converges on
  the same exception and the same terminal state rather than creating a second unit of work, and a
  message redelivered after a visibility timeout is reprocessed without double-counting.
- Acknowledgement is by receipt handle *after* terminal persistence, and the queue's own redrive
  policy bounds delivery attempts. The deployed queue reports `VisibilityTimeout` 30 s and
  `maxReceiveCount` 3, so a message left unacknowledged simply becomes visible again after 30 s
  and only reaches the dead-letter queue on a fourth receive; it is never silently dropped.

So the backlog is safe to drain by bringing the consumer back. No message needed to be replayed by
hand, and no exception needed to be edited.

**The equivalent manual actions**, in order, for an on-call engineer:

```shell
# 1. Confirm the consumer really is absent, not merely slow.
docker compose --profile observability --profile localstack ps worker

# 2. Bring the consumer back. This is the whole recovery for a consumer outage.
docker compose --profile observability --profile localstack start worker
#    equivalently: ./.tools/bin/uv run --frozen poe worker-start
#    if the container is unhealthy rather than stopped:
#    ./.tools/bin/uv run --frozen poe restart     # restarts api and worker, no rebuild

# 3. Watch the backlog drain, from the queue itself, not from the worker's gauges.
#    (repeat until ApproximateNumberOfMessages is 0)
aws --endpoint-url http://localhost:4566 sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/coldline-exception-jobs \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

**If messages *had* reached the dead-letter queue** (they did not here, but they can in the rare
case where a message was mid-receive when the container stopped, expired back onto the queue, and
exhausted its receive budget):

```shell
# 4a. Check the dead-letter depth first.
aws --endpoint-url http://localhost:4566 sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/coldline-exception-jobs-dlq \
  --attribute-names ApproximateNumberOfMessages

# 4b. Redrive: move every dead-lettered message back to the main queue and confirm recovery.
./.tools/bin/uv run --frozen poe redrive        # tests/failure/redrive_and_verify.py
```

Redrive is only safe **after** the consumer is healthy again — redriving into a queue with no
consumer just rebuilds the backlog, and redriving while the original fault persists burns the
receive budget a second time. If the same message dead-letters again after a redrive, stop
redriving: that is a poison message, and it needs its payload inspected, not another replay.

## Verification

Recovery is over only when all four of these hold at once. In my run they did.

1. **Every affected reading reached `COMPLETED`.** The lab polled
   `GET /api/v1/exceptions/{exception_id}` for each of the five `exception_id`s it submitted and
   recorded a terminal state for each. All five were `COMPLETED`, none `FAILED`:
   `exc-0e9b9baa-17e8-559a-b39e-c5b54cc85cef`, `exc-9a70606d-b87b-5b62-93e3-a649608af67a`,
   `exc-a403518c-53cc-5465-8d57-2b55d2387e6a`, `exc-60154c80-a683-5b33-a954-69908463bd79`,
   `exc-77f1d22c-1f15-5a52-945d-09a36f5234ac`. Counting five terminal states and checking each one
   is `COMPLETED` is the check — "the container is running again" is not.
2. **Both queue depths are back at zero.** My second terminal read main = 0 visible / 0 in-flight
   and dead-letter = 0 at 09:42:52, from SQS itself. The lab's own independent host-side reading
   agreed: `final_queue_depth: 0`, `final_dead_letter_depth: 0`.
3. **The worker is being scraped again.** `up{job="coldline-worker"}` returned to `1` and
   `coldline_job_queue_pending_messages` / `coldline_job_queue_dead_letter_depth` produced fresh
   samples (both `0`) at 09:42:52. Until fresh samples return, the dashboard is showing a gap, not
   a healthy system.
4. **No alert is firing, and none is pending.** Alertmanager's `/api/v2/alerts` stayed `[]`
   throughout, so `ColdlineDeadLetterQueueBacklog` neither fired nor needed to resolve.

The evidence to keep is the JSON blob `poe dev-failure-lab` printed, and its exit code `0`:

```json
{
  "fault": "consumer_outage",
  "target_service": "worker",
  "worker_stopped_seconds": 10.0,
  "exception_ids": [
    "exc-0e9b9baa-17e8-559a-b39e-c5b54cc85cef",
    "exc-9a70606d-b87b-5b62-93e3-a649608af67a",
    "exc-a403518c-53cc-5465-8d57-2b55d2387e6a",
    "exc-60154c80-a683-5b33-a954-69908463bd79",
    "exc-77f1d22c-1f15-5a52-945d-09a36f5234ac"
  ],
  "queue_depth_while_stopped": 5,
  "dead_letter_depth_while_stopped": 0,
  "redriven_messages": 0,
  "states": {
    "exc-0e9b9baa-17e8-559a-b39e-c5b54cc85cef": "COMPLETED",
    "exc-9a70606d-b87b-5b62-93e3-a649608af67a": "COMPLETED",
    "exc-a403518c-53cc-5465-8d57-2b55d2387e6a": "COMPLETED",
    "exc-60154c80-a683-5b33-a954-69908463bd79": "COMPLETED",
    "exc-77f1d22c-1f15-5a52-945d-09a36f5234ac": "COMPLETED"
  },
  "final_queue_depth": 0,
  "final_dead_letter_depth": 0,
  "recovery_seconds": 4.1,
  "outcome": "recovered"
}
```

Recovery took 4.1 s of draining after the worker was started, for a 10 s outage and five readings.
That number is this stack's drain time at this depth; it is not a capacity or latency claim. What
this local run cannot establish about the same worker as an Amazon ECS service is recorded in
[`docs/fidelity/JobQueue.md`](../fidelity/JobQueue.md).
