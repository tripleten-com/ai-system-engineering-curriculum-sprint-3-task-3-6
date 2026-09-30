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

Task 3.6's failure lab took the `worker` away with `docker compose stop worker`, let five readings
build a backlog on `coldline-exception-jobs`, and brought it back with `docker compose start
worker`. Recovery was complete: all five exceptions reached `COMPLETED`, both depths returned to
zero, and `recovery_seconds` was 6.3. That is evidence about *this* container on *this* host. It is
not evidence about the same worker running as an Amazon ECS service, for the following reasons.

- ECS-01: Compose scheduled one `worker` container on one Docker host, with the image already local
  and no placement decision to make; the run therefore shows nothing about how an ECS cluster
  chooses an instance for a replacement task, how placement strategies and constraints spread or
  bin-pack tasks across an Availability Zone, or whether a capacity provider would have had room to
  place the task at all — a cluster with no free CPU or memory reservation would simply leave the
  task `PROVISIONING` and the backlog would keep growing, which is a failure mode this stack cannot
  produce.
- ECS-02: The worker came back only because the lab script issued `docker compose start worker`; an
  operator action stood in for a scheduler. Compose has no desired count and never noticed the
  container was gone. An ECS service scheduler detects a stopped or failed task against its desired
  count and replaces it on its own, with its own detection interval, task-launch latency, image
  pull, and restart backoff after repeated failures — none of which I observed or measured. My 19–20
  second `up{job="coldline-worker"} == 0` gap is the length of a scripted pause plus a restart, not
  a measurement of ECS mean time to replacement.
- ECS-03: The backlog was five messages and one consumer, and it drained in seconds, so nothing in
  this run exercised scale-out. ECS Service Auto Scaling driven by a queue-depth target-tracking or
  step-scaling policy on `ApproximateNumberOfMessages` (or backlog-per-task) was never configured,
  never triggered, and never measured; how many concurrent worker tasks would be needed to drain a
  real peak-season backlog within the dispatcher's tolerance, and how long a scale-out would take to
  become effective, remain unproven. The run also says nothing about whether multiple concurrent
  consumers would still produce exactly one summary per reading at that scale, since only one
  consumer ever ran.
- ECS-04: `poe start` uses Compose's own `healthcheck` and `--wait`, which only gate the local
  startup sequence. They are not an ECS health-check grace period, and there is no load balancer in
  this topology at all: the worker is scraped directly at `worker:9100`. So this run establishes
  nothing about an ALB or NLB target-group health check marking a task healthy or unhealthy, about a
  grace period suppressing those checks while a task warms up, or about deregistration delay
  draining in-flight work from a task being replaced — the local equivalent of that draining is only
  SQS's own 30 s visibility timeout returning an unacknowledged message, which is a different
  mechanism.

One further honest limit that spans all four codes: LocalStack SQS, Compose networking, and a single
host give no evidence about the IAM task role an ECS task would need to call SQS, about
cross-Availability-Zone behaviour, or about what the same fault costs in a managed account.
