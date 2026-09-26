# Runbook — bounded consumer outage on the exception worker

Scope: the `worker` container that consumes `coldline-exception-jobs` stops (crash, deploy,
operator action, host loss) while the API keeps accepting readings. Written from my own
`poe dev-failure-lab` run on 2026-09-26, watched live from a second terminal. All times are UTC
from that run.

Fault as observed: `docker compose stop worker`, five readings submitted through
`POST /api/v1/readings` with no consumer, ~10 s scripted pause, `docker compose start worker`,
then recovery. Evidence blob at the end of this file's Verification section.

## Detection

What tells you first is the **queue**, not the dashboard.

- Baseline at 17:01:48, before the fault: main queue `ApproximateNumberOfMessages=0`,
  `ApproximateNumberOfMessagesNotVisible=0`, dead-letter queue `0`/`0`,
  `up{job="coldline-worker"}=1`, and `coldline_job_queue_stream_length`,
  `coldline_job_queue_pending_messages`, `coldline_job_queue_dead_letter_depth` all `0`.
- At 17:01:58, ~4 s after the worker was stopped and the five readings were accepted, my own
  host-side query of LocalStack SQS returned main queue **visible = 5, in-flight = 0**,
  dead-letter queue **0 / 0**. A second sample at 17:02:08 read the same: **5 / 0 / 0 / 0**.
  The lab's own independent reading agreed: `while stopped: queue_depth=5 dead_letter_depth=0`.
- The API kept accepting readings throughout and returned an `exception_id` for each of the
  five (the lab raises on any non-success status and it did not). Nothing upstream looked
  broken, which is exactly why queue depth is the signal.
- Prometheus showed the outage as **failed scrapes**: `up{job="coldline-worker"}` was `0` for
  nine consecutive 2-second scrapes, 17:01:55 through 17:02:11.
- The worker-exported gauges did not show the backlog, because the worker exports them. There is
  a **hole with no samples at all** in `coldline_job_queue_stream_length`,
  `coldline_job_queue_pending_messages`, and `coldline_job_queue_dead_letter_depth` from
  17:01:53 to 17:02:13 — about 20 s of missing data for a 10 s scripted pause, because the
  container restart and the next scrape both add time. A missing sample is not a measurement
  of zero, and the Grafana queue panel (which plots `coldline_job_queue_pending_messages`, the
  in-flight count, not the waiting backlog) stayed flat at 0 and then went briefly to 1 during
  the drain — it never plotted the five waiting messages. Do not conclude "queue is empty"
  from that panel during an outage.
- Alerting stayed silent, correctly. `ColdlineDeadLetterQueueBacklog`
  (`coldline_job_queue_dead_letter_depth > 0 for 15s`, Task 3.4's settled window) never went
  pending: Prometheus `/api/v1/alerts` returned `{"alerts":[]}` and Alertmanager
  `/api/v2/alerts` returned `[]` right after the run. That is the expected behaviour — nothing
  was dead-lettered, so the user-impact alert had nothing to fire on. There is no alert on
  main-queue backlog in this checkpoint, so **a consumer outage of this length is not paged;
  it is found by looking at the queue or at `up`.**

**First action on any suspicion:** ask the queue itself, not the worker's metrics.

```shell
# main-queue and dead-letter depth, taken from the host, independent of the worker
docker compose --profile observability --profile localstack ps            # is worker up?
# Prometheus: failed scrapes are the worker-independent signal
#   up{job="coldline-worker"} == 0
```

## Diagnosis

The three depths together separate the causes. What I read while the worker was gone:

| Signal | Observed | Reads as |
|---|---|---|
| main queue visible | 5, growing with submissions, then flat | work is arriving and not being taken |
| main queue in-flight (`NotVisible`) | 0 | nobody is *receiving* — no consumer at all |
| dead-letter depth | 0 | no message has exhausted `maxReceiveCount` |
| `up{job="coldline-worker"}` | 0 | the process is not there to be scraped |

Growing visible depth with **zero in-flight and zero dead-letter depth** is an *absent*
consumer, not a failing one. Contrast:

- **Dependency outage** (the worker is up but Postgres, LocalStack, or the model provider is
  not): the worker keeps receiving, so in-flight goes **non-zero**, `up` stays `1`, messages
  return after the 30 s visibility timeout, and after `maxReceiveCount = 3` receives they start
  landing in the dead-letter queue. In-flight > 0 is the discriminator.
- **Poison message**: the queue keeps draining except for one identity that cycles
  receive → visibility timeout → receive and then dead-letters on its own, while depth otherwise
  falls. Dead-letter depth rises while main depth does not.

Confirm before acting, with two commands I did not have running during this bounded lab (I was
sampling the queue instead) but that belong in the next on-call's hands:

```shell
docker compose --profile observability --profile localstack ps worker        # expect: exited
docker compose --profile observability --profile localstack logs --tail 50 worker
```

If the logs simply stop, with no application error before the gap, the process was taken away —
it did not crash on the work, and a restart is the right move. If instead the logs show repeated
adapter errors while `up` stays `1` and in-flight is non-zero, treat it as a dependency outage
and fix the dependency first; restarting the worker there only burns receive attempts toward
`maxReceiveCount`.

## Recovery

**What the lab did** (and what I would do by hand):

1. It restarted the single stopped service — `docker compose start worker`. Compose brought the
   `initializer` dependency up first, then started `worker`; the first successful scrape was at
   17:02:13, about 2 s after the restart.
2. The consumer immediately began draining. The first post-restart scrape recorded
   `coldline_job_queue_stream_length = 4` and `coldline_job_queue_pending_messages = 1` — four
   still waiting, one in flight — and the lab's own poll printed `recovering: terminal=0/5
   queue_depth=4 dead_letter_depth=0`. Two scrapes later both gauges were back at 0.
3. It polled for a bounded window (120 s cap) until every exception reached a terminal state and
   both depths were zero, redriving anything that had reached the dead-letter queue first.
   In my run nothing had: `redriven_messages: 0`.

**Why restarting was safe, with no duplicated work.** Nothing was lost or double-counted because
the durable `ExceptionRecord` identity, not the message, is the unit of work: `WorkerApplication`
treats a delivery for an identity that is already terminal as a safe replay and does not re-run
the summary. The messages were never received while the worker was down, so no receive attempt
was spent and the queue's own redrive policy (`maxReceiveCount = 3`, visibility timeout 30 s,
Task 3.3's settled values) had nothing to act on. Acknowledgement happens by receipt handle only
after terminal persistence, so a restart mid-processing costs at most one redelivery, not a lost
summary.

**Manual commands an on-call engineer runs** (equivalent to what the lab did):

```shell
# 1. restore the consumer
docker compose --profile observability --profile localstack start worker
#    or, from the repository root:  ./.tools/bin/uv run --frozen poe worker-start

# 2. watch the backlog drain, from the host, independent of the worker's own metrics
#    ApproximateNumberOfMessages on coldline-exception-jobs must fall to 0,
#    and ApproximateNumberOfMessages on coldline-exception-jobs-dlq must stay 0

# 3. confirm each affected reading individually
curl -s http://localhost:8000/api/v1/exceptions/<exception_id>   # expect "state":"COMPLETED"
```

**If messages had reached the dead-letter queue** (they did not in this run — that needs
`maxReceiveCount = 3` real receive attempts, which a stopped consumer never makes; only a
message that was mid-receive at the stop can spend one attempt):

```shell
# move dead-lettered messages back to the main queue and prove idempotent completion
./.tools/bin/uv run --frozen poe redrive     # tests/failure/redrive_and_verify.py
```

Redrive only after the consumer is healthy again, otherwise the messages simply cycle back.
Redrive is safe for the same reason the restart is: the redriven message carries the same
durable `exception_id`, and a replay of an already-terminal identity changes nothing. Escalate
instead of redriving in a loop if the same identity dead-letters twice — that is a poison
message, not a transport problem.

## Verification

Recovery is over when all three of these hold at once, not when the container shows `running`:

1. **Every affected reading is `COMPLETED`.** All five `exception_id`s from this run returned
   `COMPLETED` from `GET /api/v1/exceptions/{id}`:
   `exc-310ac5df-…`, `exc-f429de67-…`, `exc-06607b8e-…`, `exc-da32d07a-…`, `exc-cb291067-…`
   (full ids in the evidence blob below). `COMPLETED` means the summary was written, which is
   what "the container is running again" does not tell you.
2. **Both queue depths are back at zero.** My own host query at 17:02:19 read main queue `0`
   visible / `0` in-flight and dead-letter `0` / `0`, and stayed there through 17:03:20. The
   worker-exported `coldline_job_queue_stream_length` and `..._pending_messages` were back at 0
   by 17:02:17, and `..._dead_letter_depth` never left 0 in any sample it has.
3. **Monitoring is fresh again.** `up{job="coldline-worker"}` returned to `1` at 17:02:13 and
   stayed there — the gauges are being scraped again, so a 0 on the dashboard is now a real
   zero and not a stale value.

`ColdlineDeadLetterQueueBacklog` was not firing and had never fired: Prometheus `/api/v1/alerts`
`{"alerts":[]}` and Alertmanager `/api/v2/alerts` `[]`, captured at 17:03:18 and 17:02:22.

The lab exited `0` and printed this evidence blob, which is the record I would attach to the
incident:

```json
{
  "fault": "consumer_outage",
  "target_service": "worker",
  "worker_stopped_seconds": 10.0,
  "exception_ids": [
    "exc-310ac5df-ed22-5057-b06e-c53667d42aa8",
    "exc-f429de67-64c7-56e0-9e7d-d22463446070",
    "exc-06607b8e-ff57-53e3-985f-7a77f510bdc8",
    "exc-da32d07a-d038-5f54-b048-d6e0c411472d",
    "exc-cb291067-acf4-5c12-903d-41ea817af8d0"
  ],
  "queue_depth_while_stopped": 5,
  "dead_letter_depth_while_stopped": 0,
  "redriven_messages": 0,
  "states": {
    "exc-310ac5df-ed22-5057-b06e-c53667d42aa8": "COMPLETED",
    "exc-f429de67-64c7-56e0-9e7d-d22463446070": "COMPLETED",
    "exc-06607b8e-ff57-53e3-985f-7a77f510bdc8": "COMPLETED",
    "exc-da32d07a-d038-5f54-b048-d6e0c411472d": "COMPLETED",
    "exc-cb291067-acf4-5c12-903d-41ea817af8d0": "COMPLETED"
  },
  "final_queue_depth": 0,
  "final_dead_letter_depth": 0,
  "recovery_seconds": 4.1,
  "outcome": "recovered"
}
```

Five of five terminal, both depths zero, nothing redriven, 4.1 s from restart to full drain.

Caveat on my own observation, so the numbers are read correctly: my second-terminal sampler
took the SQS reading first and the Prometheus readings after it, about 10 s apart per round, so
within one printed sample the queue depth and the Prometheus values are a few seconds apart.
The two-sample queue reading while the worker was stopped (5 / 0 / 0) and the lab's own
independent reading are the reliable ones; the 20 s metric hole is measured from Prometheus's
own sample timestamps, not from my sampler.
