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

Task 3.6's failure lab stopped and started one Compose `worker` container for about ten seconds,
built a five-message backlog, and watched it drain in 4.1 s. That is a local fault control, not an
Amazon ECS service event. What the run establishes about a Compose container it does not establish
about the same worker running as an ECS service:

- ECS-01: Compose ran exactly one worker container on one host, placed by me and by nothing else; no
  placement strategy, bin-packing, capacity provider, subnet or availability-zone spread, task-size
  reservation, or cluster capacity shortfall was involved, so nothing in this run shows that a
  replacement task could be placed at all, or where.
- ECS-02: The consumer came back because I ran `docker compose start worker` — an operator action
  taken at a moment I chose. An ECS service scheduler notices a stopped or unhealthy task by itself
  and launches a replacement to hold the desired count, with its own detection interval, placement
  and image-pull latency, and restart backoff after repeated failures. I observed none of that
  timing; my 4.1 s recovery measures a warm local container start, not ECS mean time to replacement.
- ECS-03: The backlog peaked at five visible messages and drained through a single consumer, so
  nothing scaled. ECS Service Auto Scaling on a queue-depth or backlog-per-task target metric was
  never exercised: how quickly CloudWatch would publish that depth, at what threshold the service
  would scale out, how many concurrent consumers the desired count would reach, and whether that
  many consumers can drain a real backlog without contending on PostgreSQL or the model provider
  are all unproven here.
- ECS-04: Health here is Compose's own `healthcheck` plus `up --wait`, which only gate my local
  start-up. They are not an ECS health-check grace period, not ALB or NLB target-group health
  checks, and not deregistration draining: this run shows nothing about a task being killed for
  failing target-group checks during startup, about in-flight work being allowed to finish while a
  task drains, or about traffic being held off a task that is running but not yet ready. The worker
  also takes no inbound traffic locally, so no load-balancer behaviour was involved at all.
- ECS-05: The monitoring gap is a local artefact too. `up{job="coldline-worker"}` went to 0 for
  about 20 s and the worker's own gauges simply stopped, because Prometheus scrapes a fixed Compose
  DNS name. Under ECS a replacement task is a new task with a new address, discovered by service
  discovery or a CloudWatch agent, so the shape and length of the observability gap — and whether
  the pre-restart and post-restart series are even the same series — would differ from what I saw.
