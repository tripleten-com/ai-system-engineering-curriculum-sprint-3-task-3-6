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

Task 3.6's failure lab stopped and started one Compose `worker` container for a bounded window
(`docker compose stop worker`, ten seconds, then `docker compose start worker`), built a
five-message backlog on `coldline-exception-jobs`, and watched the backlog drain in 4.1 s with
both depths back at zero. That run is a *local fault control*, not an ECS service event. The
following are limits of that evidence: each names something the run could not establish about the
same worker running as an Amazon ECS service.

- ECS-01: Task placement is unproven. Compose ran exactly one `worker` container on one Docker
  host, chosen by nothing — there was no cluster, no placement strategy or constraint, no
  bin-packing across container instances, no capacity provider, and no Fargate capacity request.
  The run therefore shows only that *this* host had room for the process; it says nothing about
  whether an ECS cluster could find capacity to place a replacement task, how long that placement
  would take, or whether a constraint (instance type, availability zone spread, attribute) would
  refuse it.
- ECS-02: Service-scheduler restart is unproven. The consumer came back because the lab's own
  script issued `docker compose start worker`, an operator action against a container that was
  still present and merely stopped. An ECS service scheduler instead *detects* that a task stopped
  or failed and launches a replacement on its own to hold the service's desired count, with its
  own detection latency, task-launch time, image pull, and exponential restart backoff after
  repeated rapid failures. None of that machinery ran here, so the observed ten-second outage
  window is a number I chose, not a measured ECS time-to-replacement, and the run says nothing
  about whether an ECS service would recover unattended at all.
- ECS-03: Autoscaling is unproven. The backlog reached five messages and was drained by the single
  consumer that already existed; no scaling signal was produced and no second consumer was ever
  created. ECS Service Auto Scaling driven by a queue-depth target-tracking policy (for example on
  `ApproximateNumberOfMessages` per task) has its own CloudWatch metric period, alarm evaluation
  periods, cooldowns, and minimum/maximum task counts, none of which existed locally. So this run
  establishes neither that scale-out would be triggered by a real backlog, nor how many concurrent
  consumers would be needed to drain one, nor that concurrent consumers would drain it correctly
  at all — the lab never exercised more than one consumer against the queue.
- ECS-04: Health-check grace and load-balancer behaviour are unproven. Compose's container
  `healthcheck` and `up --wait` only told my own host that a container reported healthy; the lab
  and `poe start` are the only things that consumed that signal. They are not an ECS health-check
  grace period (which suppresses task replacement while a slow-starting task warms up), not ALB or
  NLB target-group health checks with their own thresholds, intervals, and unhealthy-count
  deregistration, and not connection draining / deregistration delay on the way out. The worker in
  this system is a queue consumer with no inbound listener, so no target group was involved at
  all; nothing here shows how traffic, registration, or draining would behave for an
  ECS-registered task, nor that a task failing an ALB health check would be replaced.
- ECS-05: Also out of scope of this run: IAM task-role and execution-role permission boundaries
  (the worker used static LocalStack credentials), rolling deployment and circuit-breaker
  rollback, and the CloudWatch/Container Insights telemetry an ECS operator would actually
  page on. Prometheus scraped the worker's own `/metrics` endpoint directly here, which is why
  `coldline_job_queue_pending_messages` had no samples at all while the container was stopped.
