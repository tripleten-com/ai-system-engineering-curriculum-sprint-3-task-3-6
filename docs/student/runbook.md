# Runbook — Coldline exception summaries stop moving (consumer outage)

**Scope.** One bounded outage of the `worker` (the only consumer of `coldline-exception-jobs`)
while temperature readings keep arriving. Symptom a dispatcher reports: an out-of-range shipment
was accepted but `/api/v1/exceptions/{exception_id}` still shows `QUEUED` with no `summary`.

**Observed run.** `poe dev-failure-lab` against the local stack on 2026-09-30, 00:32:26–00:33:32
UTC, with a 1 s polling loop asking SQS and Prometheus directly from the host at the same time.
All numbers below are from that run. The lab's evidence blob is quoted under **Verification**.

## Detection

What was true before the fault, for ten seconds of baseline samples (00:32:26–00:32:35): main queue
`ApproximateNumberOfMessages` = 0 with `ApproximateNumberOfMessagesNotVisible` = 0, dead-letter queue
0/0, `up{job="coldline-worker"}` = 1, and the worker's own gauges
(`coldline_job_queue_stream_length`, `coldline_job_queue_pending_messages`,
`coldline_job_queue_dead_letter_depth`) all reporting 0.

At 00:32:36 two things changed in the same sample:

- `up{job="coldline-worker"}` went to **0** in Prometheus (`http://localhost:9090`) and stayed 0
  through 00:32:55 — about 19–20 s, noticeably longer than the lab's 10 s scripted pause, because
  the container restart and the next successful scrape both add time.
- The main queue jumped to **5 visible / 0 in-flight**, asked of SQS itself from the host, and held
  at exactly 5/0 for nineteen consecutive one-second samples (00:32:36–00:32:54).

Two detection traps I hit and want the next engineer to avoid:

- **The worker's own gauges do not go to zero — they disappear.** From 00:32:36 to 00:32:55 my
  instant query for `coldline_job_queue_stream_length`, `coldline_job_queue_pending_messages`, and
  `coldline_job_queue_dead_letter_depth` returned *no series at all*: Prometheus marks them stale
  as soon as the scrape fails. A missing sample is not a measurement of zero. Every one of those
  gauges is scraped *from the worker*, so they are useless exactly when the worker is the problem.
  The queue's own `ApproximateNumberOfMessages`, asked of SQS from the host, is the only backlog
  evidence that survives the outage.
- **No alert fires, and that is correct.** Alertmanager (`http://localhost:9093`) returned an empty
  active-alert list in every one of the 65 samples, and Prometheus reported
  `ColdlineDeadLetterQueueBacklog` as `inactive` throughout. Its expression is
  `coldline_job_queue_dead_letter_depth > 0` for 15 s, and the dead-letter depth was 0 for the whole
  run, so there was nothing for it to fire on. **This outage is not covered by a page.** The only
  automatic signals an on-call engineer gets are `up{job="coldline-worker"} == 0` and the queue
  depth; the dispatcher's complaint may well arrive first.

**Detect it with:**

```shell
# from the repository root, with the stack up
./.tools/bin/uv run --frozen python -c "from tests.failure.queue_client import client, queue_url, queue_counts; s = client(); print({n: queue_counts(s, queue_url(s, name=n)) for n in ('coldline-exception-jobs', 'coldline-exception-jobs-dlq')})"
```

and `up{job="coldline-worker"}` in Prometheus. Wrap the first in a loop to see whether the backlog
is growing, flat, or draining.

## Diagnosis

The three-number fingerprint that separates the three failures that look alike from the outside is
**visible depth / in-flight depth / dead-letter depth**, all read from SQS rather than from the worker.

| What is wrong | visible | in-flight (`NotVisible`) | dead-letter | `up{job="coldline-worker"}` |
|---|---|---|---|---|
| **Absent consumer** (this incident) | grows, then flat | **0** | **0** | **0** |
| Failing consumer / poison message | flat or growing | non-zero, cycling | grows after `maxReceiveCount` receives | 1 |
| Dependency outage behind a live worker | grows | non-zero | may grow | 1 |

At 00:32:36–00:32:54 I measured 5 / 0 / 0 with `up` = 0, which is the first row exactly.

Why in-flight = 0 is the decisive number: **a stopped consumer never receives.** SQS moves a message
to the dead-letter queue only after `maxReceiveCount` (3, from `compose.yaml`) *actual* receive
attempts, so an absent consumer cannot dead-letter anything by itself — messages just pile up
visible. A *failing* consumer is the opposite: it receives, fails, lets the 30 s visibility timeout
expire, receives again, and after three attempts the message lands in the dead-letter queue and
`ColdlineDeadLetterQueueBacklog` pages. A dependency outage also keeps `up` at 1 and shows in-flight
messages, because the worker is alive and holding the work it cannot finish.

So: **zero dead-letter depth plus zero in-flight plus `up` = 0 means nobody is consuming, not that
consumption is failing.** Confirm with `docker compose ps worker` and `docker compose logs worker`;
in this run `docker compose stop worker` was the cause, since the lab performed it.

Also useful for sizing the customer impact before you act: open any affected exception at
`/api/v1/exceptions/{exception_id}` and compare `accepted_at` with `updated_at`. For
`exc-88b72710-46b4-5e7d-bdb9-4b803f64bd5e` that was `2026-09-30T00:32:35.799816Z` accepted versus
`2026-09-30T00:32:54.833759Z` completed — **19.0 s** during which a dispatcher checking that
shipment saw `QUEUED` and no summary.

## Recovery

**What the lab did.** It ran `docker compose start worker`, then polled every exception and both
queue depths until all five were terminal and both depths were zero. The whole recovery took
**6.3 s** from the restart (`recovery_seconds` in the evidence blob). It also redrives anything
found in the dead-letter queue before its final check; in this run it redrove **0** messages,
because nothing was ever dead-lettered.

**What I observed while it drained.** At 00:32:55 the main queue read 1 visible / 1 in-flight — the
worker back and consuming. At 00:32:56 `up` returned to 1, the main queue was 0/0, and
`coldline_job_queue_pending_messages` briefly read 1 (and 1 again at 00:32:57) before settling at 0
from 00:32:58. That gauge only counts in-flight messages, so it showed the tail of the drain, never
the five-message backlog itself.

**The equivalent manual actions**, in order, for an on-call engineer with no lab script:

```shell
# 1. Bring the consumer back. This is the bounded action that restores service.
./.tools/bin/uv run --frozen poe worker-start
#    (equivalently: docker compose --profile observability --profile localstack start worker)

# 2. Watch the backlog drain, from SQS itself, not from the worker's gauges.
./.tools/bin/uv run --frozen python -c "from tests.failure.queue_client import client, queue_url, queue_counts; s = client(); print({n: queue_counts(s, queue_url(s, name=n)) for n in ('coldline-exception-jobs', 'coldline-exception-jobs-dlq')})"

# 3. ONLY IF the dead-letter depth is non-zero: move every dead-lettered message back
#    to the main queue. One run moves all of them.
./.tools/bin/uv run --frozen poe redrive
```

**Why restarting is safe and does not duplicate work.** Nothing is lost while the consumer is away:
the messages sit visible on the queue, and the readings were already durably accepted by the API
(each has an `ExceptionRecord` in `QUEUED`). When the worker returns it processes each message
through the idempotent `ExceptionRecord` state machine, which is keyed on the reading's own
identity, so a message delivered twice — by a visibility-timeout redelivery, by a redrive, or by a
dispatcher resubmitting the same `reading_id` — converges on the same single terminal record and
one summary, rather than producing a second one. Acknowledgement happens by receipt handle only
*after* terminal persistence, so a message is never deleted for work that did not land. That is why
step 3 is safe to run even if you are not sure whether a message was already processed.

**If messages had reached the dead-letter queue.** They cannot arrive from an absent consumer, but
they can arrive from the rare edge case where a message was mid-receive when the container stopped,
expired back onto the queue, and burned receive attempts. In that case
`ColdlineDeadLetterQueueBacklog` fires after 15 s, and the fix is `poe redrive` (step 3): it
receives from the dead-letter queue, re-sends each body to the main queue, and deletes the original,
which is exactly what the lab's own redrive step does. Before redriving repeatedly, though, check
the worker's logs — a message that keeps returning to the dead-letter queue is a poison message, a
different incident, and redriving it in a loop only re-runs the same failure.

## Verification

Recovery is over only when all four of these are true. In this run they were:

1. **Every affected reading reached `COMPLETED`.** All five `exception_ids` from the lab's evidence
   blob returned `state: "COMPLETED"` — not `FAILED`, not still `QUEUED`. I spot-checked one
   record directly at `/api/v1/exceptions/exc-88b72710-46b4-5e7d-bdb9-4b803f64bd5e`: state
   `COMPLETED`, with a non-null `summary` ("…exceeded the upper handling bound by 3.4 C…") and
   `failure_reason: null`.
2. **The main queue returned to zero.** `final_queue_depth` = 0, and my independent poll read 0
   visible / 0 in-flight continuously from 00:32:56 to the end of the window at 00:33:32.
3. **The dead-letter queue was zero and stayed zero.** `final_dead_letter_depth` = 0, with
   `redriven_messages` = 0; my own samples read 0/0 in every one of the 65 samples, before, during,
   and after.
4. **Monitoring came back, and no alert was left firing.** `up{job="coldline-worker"}` = 1 from
   00:32:56 on, the three worker gauges produced fresh samples again (all 0), and Alertmanager
   still listed no active alerts, with `ColdlineDeadLetterQueueBacklog` `inactive`.

The lab's own evidence blob, which is the artifact to attach to the incident, and which exits 0
only when all of the above hold:

```json
{
  "fault": "consumer_outage",
  "target_service": "worker",
  "worker_stopped_seconds": 10.0,
  "exception_ids": [
    "exc-88b72710-46b4-5e7d-bdb9-4b803f64bd5e",
    "exc-ad2beba2-d77f-55a2-b7f9-cd02b634654d",
    "exc-e7eed7be-fc19-57c5-a78b-7b2d49c11419",
    "exc-6602dc29-edee-5b1f-89cf-a9ba6d9d7715",
    "exc-2a3f3d77-131f-5db8-ab5e-cf8766b3422f"
  ],
  "queue_depth_while_stopped": 5,
  "dead_letter_depth_while_stopped": 0,
  "redriven_messages": 0,
  "states": {
    "exc-88b72710-46b4-5e7d-bdb9-4b803f64bd5e": "COMPLETED",
    "exc-ad2beba2-d77f-55a2-b7f9-cd02b634654d": "COMPLETED",
    "exc-e7eed7be-fc19-57c5-a78b-7b2d49c11419": "COMPLETED",
    "exc-6602dc29-edee-5b1f-89cf-a9ba6d9d7715": "COMPLETED",
    "exc-2a3f3d77-131f-5db8-ab5e-cf8766b3422f": "COMPLETED"
  },
  "final_queue_depth": 0,
  "final_dead_letter_depth": 0,
  "recovery_seconds": 6.3,
  "outcome": "recovered"
}
```

**Do not verify from the worker's gauges alone.** They read 0 both when there is genuinely no
backlog and, after a restart, before they have anything to report; and on a fast drain the backlog
can vanish between two scrapes, so Prometheus may never show a non-zero sample at all. Confirm the
zeroes against SQS itself, and confirm the terminal states against the exception records.

**Customer impact to report:** five out-of-range readings had no summary for about 19 s
(`accepted_at` → `updated_at` on the record I checked). None were lost, none were duplicated, and
none needed manual resubmission.

**What this run does not tell you.** The recovery above is a Compose container start on one host.
See `docs/fidelity/JobQueue.md` for what it does not establish about the same worker running as an
ECS service — in particular, that nothing here restarted the worker *automatically*.
