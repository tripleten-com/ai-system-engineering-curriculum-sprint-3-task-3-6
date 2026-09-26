# JobQueue fidelity

Task 3.3 replaces the active supplied adapter. Redis Streams is retired as the `JobQueue`
transport; the active adapter is now LocalStack SQS, with a bound dead-letter queue. Redis
itself keeps running in `compose.yaml` only because an earlier checkpoint's own contract test
(`tests/contract/test_telemetry_repair.py`) still exercises it directly — it is no longer read
by any composition root and carries no production traffic.

The active adapter proves local publication, long-poll consumption, staleness recovery through
SQS's own visibility timeout (no application polling loop), a bounded number of delivery
attempts enforced by the queue's own redrive policy, automatic dead-letter arrival once that
bound is exceeded, and acknowledgement by receipt handle after terminal persistence.

LocalStack's SQS emulation is not equivalent to managed Amazon SQS. This local implementation
does not claim IAM enforcement, cross-region replication, at-least-once delivery under real
network partitions, managed-service durability guarantees, availability, or cost. Queue and
dead-letter-queue provisioning happens once, from the initializer, against a single LocalStack
container with no replication, backup, authentication, or production availability guarantee.
`ApproximateNumberOfMessages` and `ApproximateReceiveCount` are approximations, as their names
say; they are exposed only as local diagnostics and exercise evidence, not as an exact count of
outstanding work.

The redrive policy's `maxReceiveCount` is student-configured (`queue_max_receive_count` in
`compose.yaml`, bounded to `[1, 10]`). A value of `1` is within that Field's bounds but is a
functionally wrong choice: it gives a delivery zero tolerance for a single transient failure,
redirecting to the dead-letter queue on the very next receive instead of allowing one retry.
`tests/contract/runtime_adapters.py` asserts this directly against the deployed production
queue, not only against its own throwaway exercise queue.

## ECS fidelity limits (Task 3.6)

Task 3.6's failure lab stopped and started one Compose `worker` container for about ten seconds
on one developer machine, and watched five messages accumulate on `coldline-exception-jobs` and
drain again in 4.1 seconds. That is a local fault control, not an Amazon ECS service event. The
same worker running as an ECS service is unproven here in the following specific ways.

- ECS-01: Compose ran exactly one `worker` container on one host, placed by nothing; no task
  definition, placement strategy or constraint, bin-packing across container instances,
  capacity provider, Fargate capacity, subnet or AZ spread, or per-task CPU/memory reservation
  was involved, so nothing in this run shows that a replacement task could be placed at all, or
  that placement would spread consumers the way a cluster would.
- ECS-02: `docker compose start worker` was my own operator action against a container that was
  still there, and it took effect in about two seconds. An ECS service scheduler instead detects
  a stopped or failed task, decides to replace it to hold the desired count, and starts a new
  one with its own detection latency, image pull, placement wait, and restart backoff — none of
  which I could observe or measure locally. The 4.1-second recovery here therefore says nothing
  about time-to-recovery under the ECS scheduler.
- ECS-03: The backlog of five messages was far too small and too short-lived to trigger, or to
  test, any scaling reaction; the consumer count was fixed at one for the whole run. ECS Service
  Auto Scaling on a queue-depth target-tracking policy, the CloudWatch alarm period and
  cooldowns it would use, and how many concurrent consumers it would take to drain a real
  backlog, are all unproven by this evidence.
- ECS-04: Compose's own `healthcheck` and the `--wait` flag on `poe start` only gate local
  container startup; the worker here sits behind no load balancer and receives no inbound
  traffic. This run therefore proves nothing about an ECS health-check grace period, ALB or NLB
  target-group health-check thresholds, target registration and deregistration delay, or
  connection draining during a deployment — nor about a rolling deployment's minimum-healthy
  and maximum-percent behaviour, which never applied here.
- ECS-05: The ~20-second hole in the worker-exported queue gauges during the outage is an
  artifact of this local Prometheus scraping one static target that disappeared. On ECS the
  replacement task is a new task ID and usually a new address, so metric continuity depends on
  service discovery, target relabelling, and staleness handling that this single-container setup
  never exercised; a gap of this shape is not evidence about observability during an ECS task
  replacement.
