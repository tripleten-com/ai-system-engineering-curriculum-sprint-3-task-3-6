# Runbook — bounded consumer outage on `coldline-exception-jobs`

Scope: the `worker` container stops consuming while the API keeps accepting readings. Written from
my own `poe dev-failure-lab` run on 2026-09-27 (worker stopped 09:42:05Z–09:42:22Z UTC, five
readings submitted with the consumer absent). Times below are wall-clock UTC from that run; queue
numbers come from a read-only polling loop that asked LocalStack SQS directly, once every 1.5 s,
from a second terminal, so they are independent of the worker's own metrics.

## Detection

What I saw first was the queue, not the dashboard.

- The main queue `coldline-exception-jobs` went from `ApproximateNumberOfMessages=0` to `5` at
  09:42:06Z and stayed at `5` for ten consecutive samples, through 09:42:20Z, while
  `ApproximateNumberOfMessagesNotVisible` (in-flight) stayed at `0` the whole time. Depth that
  climbs and then holds, with nothing in flight, is the signal: work is arriving and nothing is
  picking it up.
- Prometheus recorded the outage as failed scrapes: `up{job="coldline-worker"}` was `1` through
  09:42:00Z, `0` at 09:42:05Z, 09:42:10Z, 09:42:15Z and 09:42:20Z, and `1` again from 09:42:25Z.
- The worker's own gauges — `coldline_job_queue_stream_length`,
  `coldline_job_queue_pending_messages`, `coldline_job_queue_dead_letter_depth` — simply have **no
  samples** between 09:42:00Z and 09:42:25Z. They are exported by the worker, so when the worker is
  gone the series stops rather than reporting zero. A missing sample is not a measurement of zero,
  and the Grafana queue panel (which plots `pending_messages`, i.e. in-flight only) therefore shows
  a flat line at zero and then a gap — it never showed my five waiting messages. In this run
  `pending_messages` was `0` at every sample that exists, before and after the outage: the five
  messages were drained between two 5 s scrapes, so the backlog was never visible in Prometheus at
  all. Do not treat that panel as the backlog signal.
- The gap in monitoring (≈25 s of missing worker samples) was longer than the lab's 10 s pause,
  because the container had to start, become healthy, and be scraped again before a fresh sample
  appeared.
- The Task 3.4 alert `ColdlineDeadLetterQueueBacklog`
  (`coldline_job_queue_dead_letter_depth > 0 for 15s`) did **not** fire, and Alertmanager's
  `/api/v2/alerts` returned an empty list throughout. That is correct behaviour, not a missed
  detection: nothing was dead-lettered, so there was nothing for that alert to report. A consumer
  outage of this shape is invisible to the only alert this system has — which is exactly why the
  first-line detection signal has to be queue depth queried from the queue itself.

## Diagnosis

Three candidate causes look similar from a distance; the queue attributes tell them apart.

- **Absent consumer (what this was).** Visible depth grows and then holds (`5`), in-flight is `0`,
  dead-letter depth is `0`, and `up{job="coldline-worker"}==0`. Nobody is calling `ReceiveMessage`,
  so no delivery attempt is being counted and no message can reach the dead-letter queue. SQS only
  redrives after `maxReceiveCount` (here `3`) *actual* receives, so a stopped consumer cannot
  dead-letter anything by itself. Confirm with `docker compose ps worker`.
- **Failing consumer / poison message.** The consumer is up (`up==1`, container healthy) but
  in-flight is non-zero and oscillates as messages become visible again after the 30 s visibility
  timeout, `ApproximateReceiveCount` climbs, and dead-letter depth starts rising. Depth falling
  into the dead-letter queue rather than to `COMPLETED` is the distinguishing mark.
- **Dependency outage.** The consumer is up and receiving — in-flight non-zero — but exceptions
  stay in a non-terminal state and worker logs show errors against the failing dependency
  (PostgreSQL, LocalStack S3/SQS, the model provider). Backlog grows *with* in-flight activity,
  which an absent consumer never shows.

For this incident, the evidence was unambiguous: depth `5`, in-flight `0`, dead-letter `0`, worker
scrape down. Absent consumer. I also checked that the API was still healthy and accepting readings
— all five `POST /api/v1/readings` calls returned an `exception_id` — which rules out an ingest
problem and confirms the loss is confined to the consuming side.

## Recovery

**What the lab did.** It ran `docker compose start worker`, restoring the consumer, and then
polled both queue depths; had anything been in the dead-letter queue it would have received,
re-sent to the main queue, and deleted each message (the same receive/send/delete shape as
`tests/failure/redrive_and_verify.py`) before its final check. In my run
`redriven_messages` was `0` — nothing ever reached the dead-letter queue, so no redrive was needed.
The backlog drained on its own: at 09:42:22Z the queue read `3` visible with `1` in flight, and by
09:42:23Z it was `0`. Recovery took 4.1 s from worker restart to all five exceptions terminal.

**The manual equivalent**, what I would run by hand as on-call, from the repository root:

```shell
docker compose ps worker                      # confirm the consumer is the thing that is gone
docker compose start worker                   # or: docker compose up -d worker (recreates it)
docker compose logs -f --tail=50 worker       # watch it re-attach and start draining
```

and, only if messages *had* reached the dead-letter queue:

```shell
./.tools/bin/uv run --frozen poe redrive      # tests/failure/redrive_and_verify.py:
                                              # receive from the DLQ, re-send to the main queue,
                                              # delete from the DLQ, then wait for COMPLETED
```

Before redriving, I would first read one dead-lettered body and its `ApproximateReceiveCount` to be
sure the message is recoverable and not a genuine poison message — redriving a poison message just
sends it around the same loop three more times. If the depth were large I would redrive in batches
and watch dead-letter depth fall to zero rather than firing one bulk move.

**Why restarting was safe.** No work is duplicated by this action. The messages were never
received, so no delivery attempt was consumed and nothing was half-processed; anything that *had*
been mid-flight would simply have become visible again after the 30 s visibility timeout with one
receive counted, still inside the `maxReceiveCount=3` budget. On top of that the `ExceptionRecord`
state machine is idempotent: a replayed `reading_id` resolves to the same `exception_id` and a
record already in a terminal state is not processed twice, so a redelivery after recovery converges
instead of writing a second summary. Restarting the consumer is therefore a bounded, reversible
action that needs no coordination and no data repair.

## Verification

I did not call the incident over on "the container is running again". The three things I checked:

1. **Every affected reading reached `COMPLETED`.** All five `exception_id`s the lab submitted were
   polled through `GET /api/v1/exceptions/{id}` until terminal, and all five reported `COMPLETED`
   — none `FAILED`:
   `exc-5456ed14-f86a-51b7-b515-c741961807c9`, `exc-65033d81-522b-50a8-b626-e1566b5d5e3f`,
   `exc-4d04df16-61a6-5f2b-aefe-00ca719713d6`, `exc-5c718937-9e12-5a78-867d-51bd00585ba0`,
   `exc-da474141-4c25-5c9a-a7f7-d1139d3dbc42`. Completing the state machine means the summary was
   written, which is the thing a running container does not prove.
2. **Both queue depths are back at zero,** read from SQS itself and not from the worker:
   `final_queue_depth=0` and `final_dead_letter_depth=0`, and my independent polling loop agreed —
   `0/0/0/0` on every sample from 09:42:23Z onward. Zero on the main queue means no reading was
   left waiting; zero on the dead-letter queue means none was quietly parked instead of processed.
3. **Monitoring came back and stayed quiet.** `up{job="coldline-worker"}` returned to `1` from
   09:42:25Z, the worker's queue gauges resumed exporting, and Alertmanager still listed no alerts,
   so nothing was left firing.

The lab's own exit code was `0` and its evidence blob recorded
`"outcome": "recovered"`, `"queue_depth_while_stopped": 5`,
`"dead_letter_depth_while_stopped": 0`, `"redriven_messages": 0`, `"final_queue_depth": 0`,
`"final_dead_letter_depth": 0`, `"recovery_seconds": 4.1`. That blob, plus my second-terminal queue
samples, is the evidence for this runbook.

Note the limit of this verification: it covers the five readings this lab submitted. For a real
incident I would also bound the affected set by time — list exceptions created between the first
failed scrape and the recovery — and confirm every one of *those* is terminal, since the queue being
empty says nothing about a record that failed before it was ever enqueued.
