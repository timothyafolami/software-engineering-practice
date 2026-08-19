"""
Layer 6 Topic 7, Part 2 - The detection gap, reconstructed from telemetry.

Why Python, and only Python: the postmortem timeline is four timestamps pulled
from four independent places -- a trace store, a recording rule, the rules API
and a deploy log. None of it executes in your service.

What this program does
----------------------
Replays scenario X (pool exhaustion driven by the `pricing` tail: latency
explodes, errors barely move) second by second, then reconstructs the timeline
the way you would after a real incident:

  T0  first user-visible harm   the earliest SLO-breaching request that a
                                trace was actually KEPT for
  T1  first metric deviation    the earliest evaluation at which the SLI
                                recording rule left its band
  T2  first alert               the rule's activeAt
  T3  first human action        the deploy log / chat message

  detection gap = T2 - T0        response gap = T3 - T2

Then it applies exactly one action item and re-runs the identical fault, which
is the acceptance criterion the topic demands: if you cannot state the action
item as a re-runnable test, it is a wish.

Three things this program is careful about, because each is a way to produce a
number that looks like evidence and is not:

  * T0 comes from the TRACE store, never from the alert. If you source it from
    the alert the gap is arithmetic, not evidence, and it will always look
    good.
  * Sampling is applied to the traces, so T0 is what you could actually have
    found afterwards, not what happened. The difference between those two is
    printed, because it is a finding about your sampling policy.
  * The two timestamps that cannot come from telemetry -- when a customer
    complained, and how long a human took to acknowledge a page -- are marked
    MODELLED wherever they appear. They are inputs to this simulation, not
    measurements of anything.

What to look for in the output
------------------------------
The before/after table at the end. The detection gap falls because of one
change to one rule, and the size of the fall is the return on the whole layer.
Then look at the sampling block: at this traffic rate head sampling costs you
seconds, and at a tenth of the traffic it costs you minutes -- on the very
trace that establishes when the incident began.
"""
import random

# --- Traffic and the incident ------------------------------------------------
REQUESTS_PER_SECOND = 100
INCIDENT_SECONDS = 40 * 60
RAMP_SECONDS = 300           # the pricing tail does not arrive all at once
PEAK_SLOW_RATIO = 0.62       # fraction of requests past the SLO threshold
PEAK_ERROR_RATIO = 0.004     # 0.4% -- below every availability threshold
BASELINE_SLOW_RATIO = 0.0001
BASELINE_ERROR_RATIO = 0.0002

# --- The SLO -----------------------------------------------------------------
TARGET = 0.999
LATENCY_THRESHOLD_MS = 300

# --- Evaluation cadence ------------------------------------------------------
RECORDING_RULE_INTERVAL = 15   # seconds
ALERT_EVAL_INTERVAL = 30       # seconds
SLI_WINDOW = 300               # 5 minutes

# --- The two modelled inputs, marked everywhere they are used ----------------
MODELLED_CUSTOMER_REPORT_S = 26 * 60   # when support escalates, if nothing pages
MODELLED_ACK_DELAY_S = 4 * 60          # pager delivery + a human reading it


def slow_ratio_at(second):
    if second < 0:
        return BASELINE_SLOW_RATIO
    ramp = min(1.0, second / RAMP_SECONDS)
    return BASELINE_SLOW_RATIO + (PEAK_SLOW_RATIO - BASELINE_SLOW_RATIO) * ramp


def error_ratio_at(second):
    if second < 0:
        return BASELINE_ERROR_RATIO
    ramp = min(1.0, second / RAMP_SECONDS)
    return BASELINE_ERROR_RATIO + (PEAK_ERROR_RATIO - BASELINE_ERROR_RATIO) * ramp


def replay(seed=20260818):
    """One run of the fault. Returns per-second counts and the trace records."""
    rng = random.Random(seed)
    seconds = []
    for second in range(INCIDENT_SECONDS):
        slow_p = slow_ratio_at(second)
        error_p = error_ratio_at(second)
        slow = sum(1 for _ in range(REQUESTS_PER_SECOND) if rng.random() < slow_p)
        errors = sum(1 for _ in range(REQUESTS_PER_SECOND) if rng.random() < error_p)
        seconds.append({"slow": slow, "errors": errors, "total": REQUESTS_PER_SECOND})
    return seconds


def first_slow_request(seconds):
    """Ground truth: the first second in which any request breached the SLO."""
    for second, row in enumerate(seconds):
        if row["slow"] > 0:
            return second
    return None


def first_sampled_slow_trace(seconds, sample_ratio, seed=7):
    """T0 as you could actually find it: the first KEPT trace over the threshold.

    Head sampling decides before the request finishes, so it is blind to
    latency by construction -- a slow request is exactly as likely to be
    dropped as a fast one.
    """
    rng = random.Random(seed)
    for second, row in enumerate(seconds):
        for _ in range(row["slow"]):
            if rng.random() < sample_ratio:
                return second
    return None


def sli_bad_ratio_series(seconds, include_latency):
    """The recording rule, evaluated every RECORDING_RULE_INTERVAL over 5m."""
    points = []
    for at in range(0, INCIDENT_SECONDS, RECORDING_RULE_INTERVAL):
        start = max(0, at - SLI_WINDOW)
        window = seconds[start:at + 1]
        if not window:
            continue
        bad = sum(r["errors"] + (r["slow"] if include_latency else 0) for r in window)
        total = sum(r["total"] for r in window)
        points.append((at, bad / total))
    return points


def first_deviation(points):
    """T1: the first evaluation at which the SLI left its band (burn rate > 1)."""
    for at, ratio in points:
        if ratio / (1 - TARGET) > 1.0:
            return at
    return None


def burn_rate_alert(seconds, include_latency, factor=14.4, for_seconds=120):
    """T2: activeAt for a multi-window burn-rate rule.

    The long window is 1h and the incident is 40 minutes, so the long-window
    burn rate is computed over whatever of the hour has elapsed plus healthy
    history -- which is what Prometheus does with a series that has been
    running for days.
    """
    long_window = 3600
    short_window = 300
    pending_since = None
    for at in range(0, INCIDENT_SECONDS, ALERT_EVAL_INTERVAL):
        def burn(window):
            start = max(0, at - window)
            observed = seconds[start:at + 1]
            bad = sum(r["errors"] + (r["slow"] if include_latency else 0)
                      for r in observed)
            # Healthy history fills the rest of the window: the service has been
            # up for days before this incident.
            missing = window - len(observed)
            bad += missing * REQUESTS_PER_SECOND * (
                BASELINE_ERROR_RATIO + (BASELINE_SLOW_RATIO if include_latency else 0))
            total = window * REQUESTS_PER_SECOND
            return (bad / total) / (1 - TARGET)

        firing = burn(long_window) > factor and burn(short_window) > factor
        if firing:
            if pending_since is None:
                pending_since = at
            elif at - pending_since >= for_seconds:
                return pending_since   # activeAt is when the condition began
        else:
            pending_since = None
    return None


def mmss(seconds_value):
    if seconds_value is None:
        return "never"
    return "T+%02d:%02d" % (seconds_value // 60, seconds_value % 60)


def timeline(seconds, include_latency, sample_ratio, label):
    truth = first_slow_request(seconds)
    t0 = first_sampled_slow_trace(seconds, sample_ratio)
    t1 = first_deviation(sli_bad_ratio_series(seconds, include_latency))
    t2 = burn_rate_alert(seconds, include_latency)
    paged = t2 is not None
    if not paged:
        t2 = MODELLED_CUSTOMER_REPORT_S
    t3 = t2 + MODELLED_ACK_DELAY_S
    return {
        "label": label,
        "truth": truth,
        "t0": t0,
        "t1": t1,
        "t2": t2,
        "t3": t3,
        "paged": paged,
        "detection_gap": None if t0 is None else t2 - t0,
        "response_gap": t3 - t2,
    }


def print_timeline(result):
    paged_note = ("the burn-rate rule" if result["paged"]
                  else "a customer support escalation  [MODELLED]")
    print("  %-28s %-10s %s" % ("", "timestamp", "source"))
    print("  %-28s %-10s %s" % ("-" * 28, "-" * 10, "-" * 46))
    print("  %-28s %-10s %s" % ("T0 first harmed request", mmss(result["t0"]),
                                "earliest KEPT trace over the SLO threshold"))
    print("  %-28s %-10s %s" % ("T1 first metric deviation", mmss(result["t1"]),
                                "SLI recording rule, 5m window, every 15s"))
    print("  %-28s %-10s %s" % ("T2 first alert", mmss(result["t2"]),
                                paged_note))
    print("  %-28s %-10s %s" % ("T3 first human action", mmss(result["t3"]),
                                "deploy log / chat  [MODELLED ack delay]"))
    print("  %-28s %-10s" % ("-" * 28, "-" * 10))
    print("  %-28s %-10s" % ("detection gap (T2 - T0)",
                             "%d min %02d s" % (result["detection_gap"] // 60,
                                                result["detection_gap"] % 60)))
    print("  %-28s %-10s" % ("response gap  (T3 - T2)",
                             "%d min %02d s" % (result["response_gap"] // 60,
                                                result["response_gap"] % 60)))


def main():
    print("Layer 6 Topic 7 Part 2 - the detection gap for scenario X, from telemetry")
    print("scenario X: the `pricing` tail exhausts the pool. Latency explodes,")
    print("            errors move from 0.02%% to %.1f%% -- under every threshold."
          % (100 * PEAK_ERROR_RATIO))
    print("=" * 78)

    seconds = replay()
    total_requests = sum(r["total"] for r in seconds)
    total_slow = sum(r["slow"] for r in seconds)
    total_errors = sum(r["errors"] for r in seconds)

    print()
    print("What actually happened (ground truth, %s requests over %d minutes)"
          % (f"{total_requests:,}", INCIDENT_SECONDS // 60))
    print("-" * 78)
    print("  requests slower than %d ms   %s  (%.1f%%)"
          % (LATENCY_THRESHOLD_MS, f"{total_slow:,}", 100 * total_slow / total_requests))
    print("  requests returning 5xx      %s  (%.2f%%)"
          % (f"{total_errors:,}", 100 * total_errors / total_requests))
    print("  first request over the SLO threshold: %s"
          % mmss(first_slow_request(seconds)))

    # -----------------------------------------------------------------------
    print()
    print("BEFORE: the SLI covers availability only")
    print("-" * 78)
    print("  This is not a strawman. Availability-only is where most SLO rollouts")
    print("  stop, because 5xx ratio is the easy counter to get right first.")
    print()
    before = timeline(seconds, include_latency=False, sample_ratio=0.10,
                      label="availability-only SLI, 10% head sampling")
    print_timeline(before)
    print()
    if not before["paged"]:
        print("  No rule fired at all. The 5xx ratio peaked at %.2f%%, and every"
              % (100 * PEAK_ERROR_RATIO))
        print("  availability rule you have is looking for something an order of")
        print("  magnitude bigger. The incident was found by a person, and every")
        print("  number after T2 in that table is a modelled guess about people.")

    # -----------------------------------------------------------------------
    print()
    print("THE ACTION ITEM (exactly one, and it is a test)")
    print("-" * 78)
    print("  Put latency in the SLI: a request is bad if it 5xx'd OR took longer")
    print("  than %d ms, and run the same burn-rate rules over that ratio." % LATENCY_THRESHOLD_MS)
    print()
    print("  Acceptance criterion: re-run this identical fault and show a smaller")
    print("  T2 - T0. Not 'improve latency alerting'. Not 'consider adding'. A")
    print("  re-runnable test with a number that has to go down.")

    # -----------------------------------------------------------------------
    print()
    print("AFTER: the same fault, the same seed, one changed SLI")
    print("-" * 78)
    after = timeline(seconds, include_latency=True, sample_ratio=0.10,
                     label="availability+latency SLI, 10% head sampling")
    print_timeline(after)

    print()
    print("  %-36s %-14s %-14s %s" % ("", "before", "after", "change"))
    print("  %-36s %-14s %-14s %s" % ("-" * 36, "-" * 14, "-" * 14, "-" * 12))
    print("  %-36s %-14s %-14s %s"
          % ("paged by a rule?", "no" if not before["paged"] else "yes",
             "yes" if after["paged"] else "no", ""))
    print("  %-36s %-14s %-14s -%.0f%%"
          % ("detection gap (T2 - T0)",
             "%d min" % (before["detection_gap"] // 60),
             "%d min %02ds" % (after["detection_gap"] // 60,
                               after["detection_gap"] % 60),
             100 * (1 - after["detection_gap"] / before["detection_gap"])))
    print("  %-36s %-14s %-14s %s"
          % ("requests harmed before detection",
             f"{harmed_before(seconds, before['t2']):,}",
             f"{harmed_before(seconds, after['t2']):,}",
             "-%s" % f"{harmed_before(seconds, before['t2']) - harmed_before(seconds, after['t2']):,}"))
    print()
    print("  The instrumentation did not change. The dashboards did not change.")
    print("  One rule now watches the promise the incident was actually breaking.")

    # -----------------------------------------------------------------------
    print()
    print("WHERE T1 SITS, AND WHAT THAT TELLS YOU TO FIX")
    print("-" * 78)
    print("  T1 between T0 and T2 means the telemetry saw it and the RULES were")
    print("  slow. T1 late means no rule change helps and the instrumentation is")
    print("  the problem. This run:")
    print()
    for result in (before, after):
        gap_to_t1 = None if result["t1"] is None else result["t1"] - result["t0"]
        print("    %-44s T0 %s   T1 %s   T2 %s"
              % (result["label"], mmss(result["t0"]), mmss(result["t1"]),
                 mmss(result["t2"])))
        if result["t1"] is not None and gap_to_t1 is not None:
            print("      T1 - T0 = %d s: the SLI recording rule left its band %d s after"
                  % (gap_to_t1, gap_to_t1))
            print("      the first harmed request, so the data was there the whole time.")
    print()
    print("  In both runs the telemetry is fine and the rules are the variable.")
    print("  That is the most common shape, and it is the cheapest to fix -- which")
    print("  is worth knowing before anyone proposes an instrumentation project.")

    # -----------------------------------------------------------------------
    print()
    print("THE SAMPLING FOOTNOTE, WHICH IS ITS OWN FINDING")
    print("-" * 78)
    truth = first_slow_request(seconds)
    print("  %-34s %-16s %s" % ("sampler", "T0 you can find", "later than the truth by"))
    print("  %-34s %-16s %s" % ("-" * 34, "-" * 16, "-" * 24))
    for ratio, label in ((1.0, "100% (or tail sampling)"),
                         (0.10, "10% head sampling"),
                         (0.01, "1% head sampling")):
        found = first_sampled_slow_trace(seconds, ratio)
        delay = "-" if found is None else "%d s" % (found - truth)
        print("  %-34s %-16s %s" % (label, mmss(found), delay))
    print()
    print("  At %d req/s the cost of head sampling is small, because the incident"
          % REQUESTS_PER_SECOND)
    print("  produces slow requests faster than the sampler can miss them. The")
    print("  arithmetic that governs it: a 1-in-N sampler drops N-1 slow requests")
    print("  on average before keeping one, so the delay is (N-1) / (slow requests")
    print("  per second). At 1% and this traffic that is ~99 / 30 = a few seconds.")
    print("  At 1% on a service doing 5 req/s with a 20% slow ratio it is ~99 / 1")
    print("  = a minute and a half of an incident you cannot prove happened.")
    print()
    print("  Head sampling is blind to latency by construction: the decision is")
    print("  made before the duration exists. A head-sampled service can be")
    print("  missing the very trace that establishes T0 -- so a postmortem that")
    print("  needs T0 is an argument for tail sampling, and that is a finding")
    print("  about sampling policy rather than a broken experiment.")

    # -----------------------------------------------------------------------
    print()
    print("THE POSTMORTEM SKELETON THIS PRODUCES")
    print("-" * 78)
    print("  Contributing factors (plural, and none of them a person):")
    print("    1. The SLI covered availability only, so a latency-only")
    print("       degradation could not move it.")
    print("    2. `pricing`'s tail is a dependency property with no timeout on")
    print("       our side, so its p99 became our p50.")
    print("    3. The pool has no wait-time metric, so the saturation that")
    print("       transmitted the tail into our latency was invisible (Topic 5).")
    print("    4. Head sampling at 10% means the trace that establishes T0 is")
    print("       kept by luck rather than by policy.")
    print()
    print("  What specifically made detection slow: no rule watched latency.")
    print("  Action item (one): put latency in the SLI, with the acceptance")
    print("  criterion above.")
    print()
    print("  Note what is NOT in that list: nobody's name, and no 'be more")
    print("  careful'. Blameless is not a manners rule -- people describe what")
    print("  they actually did, including the part where they skipped the")
    print("  checklist, only when doing so is safe, and you cannot fix a system")
    print("  you have an inaccurate account of.")


def harmed_before(seconds, until):
    """How many requests breached the SLO before `until` seconds."""
    return sum(r["slow"] + r["errors"] for r in seconds[:min(until, len(seconds))])


if __name__ == "__main__":
    main()
