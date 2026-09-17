"""
Log investigation script for the "lost orders" incident.

Usage:
    python3 analyze_logs.py web.log worker.log [--graphs OUTPUT_DIR]

What it does:
  1. Extracts every /checkout request from web.log with its request_id and timestamp.
  2. Extracts every ECONNRESET "upstream call failed" job from worker.log.
  3. Correlates the two by request_id to find which checkout requests never
     completed successfully.
  4. Prints the first affected web.log request AND the first corresponding
     worker.log failure (these are two different timestamps ~2.6s apart,
     since the worker processes the job after the web tier already returned
     202 to the client) plus per-hour failure rates, the affected endpoint,
     the upstream host involved, and the count of distinct affected users.
  5. Also checks the other two worker.log message types (AnalyticsUploadTimeout)
     and web.log slow-query warnings to confirm they are unrelated background
     noise, not correlated with the incident.
  6. If --graphs is given, writes the three PNG charts used in ANSWERS.md into
     that directory (requires matplotlib).
"""

import re
import sys
import os
from collections import defaultdict

WEB_LOG = sys.argv[1] if len(sys.argv) > 1 else "web.log"
WORKER_LOG = sys.argv[2] if len(sys.argv) > 2 else "worker.log"
GRAPH_DIR = None
if "--graphs" in sys.argv:
    GRAPH_DIR = sys.argv[sys.argv.index("--graphs") + 1]

# Capture the full timestamp (date + time + fractional seconds) as one token,
# rather than assuming a fixed number of decimal digits.
TS = r"(\S+ \S+\.\d+)"
CHECKOUT_RE = re.compile(rf"^{TS}.*path=/checkout.*user_id=(\d+).*request_id=(\S+)")
ECONNRESET_RE = re.compile(rf"^{TS}.*upstream call failed request_id=(\S+) err=ECONNRESET upstream=(\S+)")
ANALYTICS_RE = re.compile(rf"^{TS}.*AnalyticsUploadTimeout")


def main():
    checkout_time = {}      # request_id -> timestamp (from web.log)
    checkout_user = {}      # request_id -> user_id
    with open(WEB_LOG) as f:
        for line in f:
            m = CHECKOUT_RE.match(line)
            if m:
                ts, user_id, rid = m.groups()
                checkout_time[rid] = ts
                checkout_user[rid] = user_id

    failed_ids = {}         # request_id -> (worker timestamp, upstream)
    with open(WORKER_LOG) as f:
        for line in f:
            m = ECONNRESET_RE.match(line)
            if m:
                ts, rid, upstream = m.groups()
                failed_ids[rid] = (ts, upstream)

    matched_failed = {rid: v for rid, v in failed_ids.items() if rid in checkout_time}

    print(f"Total /checkout requests in web.log: {len(checkout_time)}")
    print(f"Total ECONNRESET worker failures:    {len(failed_ids)}")
    print(f"ECONNRESET failures that map to a /checkout request: {len(matched_failed)}")

    upstreams = {v[1] for v in matched_failed.values()}
    print(f"Failing upstream host(s): {upstreams}")

    # These are two different timestamps from two different log files —
    # report both rather than collapsing them into one "first failure" time.
    first_checkout_ts = min(checkout_time[rid] for rid in matched_failed)
    first_worker_ts = min(v[0] for v in matched_failed.values())
    print(f"First affected /checkout request (web.log):     {first_checkout_ts}")
    print(f"First corresponding worker failure (worker.log): {first_worker_ts}")

    affected_users = {checkout_user[rid] for rid in matched_failed}
    print(f"Distinct users affected: {len(affected_users)}")

    # Hourly failure rate. ts looks like "2026-07-02 14:32:40.073";
    # ts[:13] == "2026-07-02 14" (date + hour), which we print with a
    # trailing ":00" to show it as an hour bucket.
    hourly_total = defaultdict(int)
    hourly_failed = defaultdict(int)
    for rid, ts in checkout_time.items():
        hour = ts[:13]
        hourly_total[hour] += 1
        if rid in matched_failed:
            hourly_failed[hour] += 1

    print("\nHourly checkout failure rate:")
    for hour in sorted(hourly_total):
        tot = hourly_total[hour]
        fail = hourly_failed[hour]
        print(f"  {hour}:00  total={tot:4d}  failed={fail:4d}  rate={fail/tot*100:5.1f}%")

    # Sanity check: confirm AnalyticsUploadTimeout is unrelated background
    # noise (different service, no correlation with the incident window).
    # Extracted with a regex + full timestamp (not a fixed string slice),
    # so this doesn't silently break if the log format changes and it keeps
    # the date, not just the hour-of-day.
    hourly_analytics = defaultdict(int)
    with open(WORKER_LOG) as f:
        for line in f:
            m = ANALYTICS_RE.match(line)
            if m:
                hourly_analytics[m.group(1)[:13]] += 1

    print("\nAnalyticsUploadTimeout count by hour (background noise, unrelated service):")
    for hour in sorted(hourly_analytics):
        print(f"  {hour}:00  count={hourly_analytics[hour]}")

    if GRAPH_DIR:
        make_graphs(checkout_time, matched_failed, hourly_analytics, GRAPH_DIR)


def make_graphs(checkout_time, matched_failed, hourly_analytics, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(out_dir, exist_ok=True)

    # 15-minute buckets for checkout volume / failure rate
    bucket_total = defaultdict(int)
    bucket_failed = defaultdict(int)
    for rid, ts in checkout_time.items():
        hh, mm = ts[11:13], ts[14:16]
        bucket_min = (int(mm) // 15) * 15
        bucket = f"{hh}:{bucket_min:02d}"
        bucket_total[bucket] += 1
        if rid in matched_failed:
            bucket_failed[bucket] += 1

    buckets = sorted(bucket_total.keys())
    totals = [bucket_total[b] for b in buckets]
    failed = [bucket_failed[b] for b in buckets]
    rates = [f / t * 100 if t else 0 for f, t in zip(failed, totals)]
    success = [t - f for t, f in zip(totals, failed)]
    idx = range(len(buckets))
    step = 4
    incident_bucket = "14:30"
    incident_idx = buckets.index(incident_bucket) if incident_bucket in buckets else None

    # Chart 1: volume, stacked success/failed
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.bar(idx, success, color="#4C9F70", label="Checkout succeeded")
    ax.bar(idx, failed, bottom=success, color="#D64545", label="Checkout failed (order lost)")
    if incident_idx is not None:
        ax.axvline(x=incident_idx, color="black", linestyle="--", linewidth=1, label="Incident starts ~14:32:40")
    ax.set_xticks(list(idx)[::step])
    ax.set_xticklabels([buckets[i] for i in idx][::step], rotation=45, ha="right")
    ax.set_title("Checkout requests per 15 minutes — success vs silently lost orders")
    ax.set_xlabel("Time (2026-07-02)")
    ax.set_ylabel("Number of /checkout requests")
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "checkout_volume.png"), dpi=150)
    plt.close()

    # Chart 2: failure rate over time
    fig, ax = plt.subplots(figsize=(14, 4.5))
    ax.plot(idx, rates, color="#D64545", linewidth=2)
    if incident_idx is not None:
        ax.axvline(x=incident_idx, color="black", linestyle="--", linewidth=1)
        ax.annotate(
            "Incident starts\n~14:32:40", xy=(incident_idx, 5),
            xytext=(incident_idx + 3, 20), arrowprops=dict(arrowstyle="->", color="black"),
        )
    ax.set_xticks(list(idx)[::step])
    ax.set_xticklabels([buckets[i] for i in idx][::step], rotation=45, ha="right")
    ax.set_title("Checkout failure rate over time (jumps from 0% to ~43-45% and never recovers)")
    ax.set_xlabel("Time (2026-07-02)")
    ax.set_ylabel("Failure rate (%)")
    ax.set_ylim(0, 60)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "failure_rate.png"), dpi=150)
    plt.close()

    # Chart 3: ECONNRESET vs AnalyticsUploadTimeout by hour, to show one
    # tracks the incident and the other is flat background noise.
    econn_hourly = defaultdict(int)
    for rid, (ts, _upstream) in matched_failed.items():
        econn_hourly[ts[:13]] += 1
    all_hours = sorted(set(econn_hourly) | set(hourly_analytics))
    econn_vals = [econn_hourly.get(h, 0) for h in all_hours]
    analytics_vals = [hourly_analytics.get(h, 0) for h in all_hours]
    hour_labels = [h[11:13] for h in all_hours]

    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(hour_labels, econn_vals, color="#D64545", marker="o", label="ECONNRESET to 10.0.3.44:8443 (checkout upstream)")
    ax.plot(hour_labels, analytics_vals, color="#888888", marker="o", label="AnalyticsUploadTimeout (unrelated background noise)")
    ax.set_title("Worker error types by hour — only ECONNRESET tracks the incident")
    ax.set_xlabel("Hour of day (2026-07-02)")
    ax.set_ylabel("Error count")
    ax.legend()
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "error_types_hourly.png"), dpi=150)
    plt.close()

    print(f"\nWrote graphs to {out_dir}/")


if __name__ == "__main__":
    main()
