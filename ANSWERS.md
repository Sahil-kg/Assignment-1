
## 1. When did the problem start?

**~14:32:40 on 2026-07-02.**

Before this point, every `/checkout` request in `web.log` has a matching
`job completed` line in `worker.log`. The first checkout request whose
worker job never completes successfully is:

```
web.log:    2026-07-02 14:32:40.073 INFO [request] method=POST path=/checkout status=202 latency_ms=36 user_id=59787 request_id=16ce72300cf58a32
worker.log: 2026-07-02 14:32:42.692 ERROR [worker] upstream call failed request_id=16ce72300cf58a32 err=ECONNRESET upstream=10.0.3.44:8443 (retries exhausted)
```

From that moment on, `ECONNRESET` failures to `upstream=10.0.3.44:8443`
appear continuously in `worker.log` until the very end of the log file
(23:59:54) — the incident never recovers within the captured window.

Note there are two slightly different timestamps depending on which file
you look at: the request itself hits `web.log` at `14:32:40.073`, and the
worker doesn't attempt (and fail) the corresponding job until `14:32:42.692`
— about 2.6s later, which is the queueing/processing delay before the
worker picks the job up. `analyze_logs.py` prints both explicitly.

## 2. Which endpoint is affected?

**`POST /checkout`.**

Every one of the 2,385 `ECONNRESET` failures in `worker.log` has a
`request_id` that maps back to a `POST /checkout` request in `web.log` —
no other endpoint appears in this set (verified: 2,385 out of 2,385 matched
IDs are `/checkout`). `POST /orders` (a separate endpoint, 7,998 requests)
is completely unaffected.

This also explains why customers saw a "successful" order: the web tier
returns `202 Accepted` for `/checkout` immediately, before the background
worker has actually processed it. The user's browser shows success, but the
order is only really placed once the worker's call to the downstream
service at `10.0.3.44:8443` completes — and that call is what's failing.

## 3. What do the failing requests have in common?

- They are all `POST /checkout` requests (see above).
- They all fail in the worker with the exact same error:
  `ECONNRESET` while calling `upstream=10.0.3.44:8443`, after retries are
  exhausted.
- The failure is **not total outage** — it's a sustained partial failure
  rate. Failure rate by hour:

| Hour | Total checkouts | Failed | Failure rate |
|---|---|---|---|
| 00:00–13:00 | 4,250 | 0 | 0% |
| 14:00 | 619 | 40 | 6.5% (incident starts mid-hour) |
| 15:00 | 651 | 267 | 41.0% |
| 16:00 | 687 | 303 | 44.1% |
| 17:00 | 765 | 341 | 44.6% |
| 18:00 | 838 | 364 | 43.4% |
| 19:00 | 789 | 344 | 43.6% |
| 20:00 | 701 | 308 | 43.9% |
| 21:00 | 463 | 202 | 43.6% |
| 22:00 | 323 | 146 | 45.2% |
| 23:00 | 156 | 70 | 44.9% |

Failure rate jumps from 0% to ~43–45% within the first hour and then holds
almost perfectly steady (43–45%) for the rest of the day. This flat,
constant-percentage pattern (rather than 100% failure, or a rate that
climbs/decays) is the signature of a **partial-capacity failure** — e.g.
one node out of a small pool behind a load balancer becoming unreachable,
so a fixed fraction of requests routed to that node fail while the rest go
through fine.

## 4. How many distinct users were affected?

**2,335 distinct users** (`user_id` values) had at least one `/checkout`
request that failed with `ECONNRESET`, out of 2,385 total failed requests
(some users hit checkout more than once during the incident, e.g. retrying
after their order appeared to succeed but never showed up).

## Bonus: Root cause hint

The worker log gives a specific, repeated signature:

```
ERROR [worker] upstream call failed request_id=... err=ECONNRESET upstream=10.0.3.44:8443 (retries exhausted)
```

- Every single failure points to the **same host and port**: `10.0.3.44:8443`
  (an internal service the checkout job calls over TLS, most likely a
  payment/order-fulfillment service).
- `ECONNRESET` means the connection was actively reset by the peer (or a
  middlebox), not a timeout — consistent with a backend process crashing,
  restarting, or being killed mid-connection, or a proxy/load-balancer
  losing one of its backend pool members.
- Retries are exhausted before giving up, and there's no fallback path —
  so the job is dropped rather than queued for a later retry, which is why
  the order silently disappears instead of erroring visibly.
- The steady ~43–45% failure rate (not 0% or 100%) is more consistent with a
  **partial** failure than a total outage — e.g. some fraction of requests
  to `10.0.3.44:8443` succeeding and some failing — but the logs don't say
  whether that host is a single instance, one member of a load-balanced
  pool, or something else. That's a plausible explanation, not something
  the logs confirm directly.
- The incident starts abruptly at a precise timestamp with no gradual
  degradation beforehand, which points to a discrete event (a deploy, a
  crash, a network/firewall change) at 14:32:40 rather than a slow resource
  leak — but again, the logs don't identify which of these it was.

**Likely root cause:** the logs strongly indicate an upstream
connectivity/availability problem with the service at `10.0.3.44:8443` that
the checkout worker calls — connections to it are being reset, and retries
are exhausted before the job gives up. The exact underlying cause (crashed
instance, bad deploy, network issue, capacity limit, etc.) and the exact
topology of that service (single host vs. a pool) cannot be determined from
`web.log` and `worker.log` alone; confirming it would need access to that
service's own logs or infrastructure metrics.

## Investigated and ruled out (unrelated)

- **`AnalyticsUploadTimeout` errors in worker.log** (793 total, ~33/hour
  all day, hitting `analytics.internal:9092`) — a completely different host
  and port, occurring at a constant rate both before and after 14:32:40.
  Not correlated with the incident; this is background noise from an
  unrelated metrics pipeline.
- **`WARN [db] slow query` entries in web.log** (1,500 total) — scattered
  throughout the entire day across `/products`, `/cart`, and session
  queries, with no concentration around 14:32:40 and no connection to
  `/checkout`. Unrelated performance noise, not a cause of lost orders.
- **`401`/`404` responses in web.log** (118 and 192 respectively) — almost
  entirely `401`s on `/api/user` (expired sessions) and `404`s on
  individual `/product/:id` lookups (bad/removed product IDs), both
  proportional to overall traffic volume and with no jump around
  14:32:40. Unrelated to checkout.
- **`POST /orders`** — a separate endpoint from `/checkout`, unaffected by
  the incident; every `/orders` job in worker.log completes normally
  throughout the day. Confirms the incident is scoped specifically to the
  checkout flow, not order creation in general.

## Investigation process

- Loaded both logs and got basic shape: line counts, status code
  distribution, and endpoint distribution in `web.log`, plus the distinct
  message types in `worker.log`.
- Noticed `web.log` has **no 5xx errors at all**, ruling out an obvious
  web-tier failure — meant the incident had to be in the async worker
  processing, invisible to the customer-facing response.
- Broke `worker.log` down by message type and found three categories:
  `job completed`, `ECONNRESET upstream call failed`, and
  `AnalyticsUploadTimeout`.
- Checked whether `AnalyticsUploadTimeout` correlated with the incident —
  it didn't (flat rate all day, different host) — and set it aside.
- Extracted every `request_id` behind the `ECONNRESET` errors and joined
  them back to `web.log` by `request_id` to find which endpoint/user they
  belonged to.
- Found 100% of `ECONNRESET` failures traced back to `POST /checkout`
  requests that had returned `202 Accepted` to the user — i.e., silent
  failures after an apparently successful response, matching the
  support report.
- Computed failure rate per hour to characterize the incident shape —
  found it jumps from 0% to ~43–45% and stays essentially flat rather than
  going to 100%, which pointed toward a partial/pool-based failure rather
  than a total outage.
- Cross-checked `WARN` (slow DB queries) and `401`/`404` responses in
  `web.log` for any correlation with the incident window — found none,
  and ruled them out as separate, pre-existing issues.
- Counted distinct `user_id` values among the failed checkout requests to
  answer the "how many users" question.
- Searched for any log line around 14:32:40 that might indicate a
  deploy/restart marker — none exists in either log; the only evidence of
  the event is the abrupt shift in the `ECONNRESET` failure rate itself.
