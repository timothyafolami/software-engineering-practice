"""
Layer 6 Topic 6 - Error budgets and multi-window burn-rate alerting.

Why Python, and only Python: everything in this topic is arithmetic the
MONITORING SYSTEM performs on counters -- recording rules, rate(), a ratio, a
multiplier. None of it executes in your service, and the same PromQL runs
identically whether the counters came from a Go binary, a JVM or this. Python
is here as the simulator: it replays incident shapes against the burn-rate
rules in seconds instead of making you wait six hours for a 6-hour window to
fill.

What this program does
----------------------
1. Derives the budget from the SLO, rather than quoting it.
2. Re-derives the standard burn-rate table (14.4 / 6 / 1) from two lines of
   arithmetic, so you can rebuild it for a window that is not 28 days.
3. Replays three incident shapes minute by minute against all three rules and
   against the naive `p99 > 500ms for 5m` rule, and reports for each: which
   alerts fired, how long after onset, how much budget the shape spent, and how
   long each alert stayed lit AFTER the incident ended.
4. Runs the three rules with and without their short windows, which is the part
   people drop and the part that stops an alert burning for six hours after a
   three-minute outage.

Everything is computed from event counts. No sampling, no RNG: each minute's
good/valid counts follow exactly from the regime that minute is in, so the
output is reproducible and every line can be checked by hand against the
arithmetic in the topic README.

The latency SLI needs a latency distribution, and this one is stated rather
than assumed: request latency is exponential with mean tau, so P(latency > t) =
exp(-t/tau) and the p99 is tau*ln(100). That single closed form is what lets
"the p99 doubled" be converted into "this fraction of requests crossed 300ms",
which is the conversion the SLI actually cares about -- and the conversion that
makes a percentile SLO and a threshold SLO behave so differently.

What to look for in the output
------------------------------
Section 5's table, one row at a time. A three-minute total outage spends 7.4% of
a month in three minutes; four hours at 8% errors spends nearly half of one and
takes ten minutes to page; a latency-only degradation with a perfectly green
availability dashboard spends 15% and is invisible to every availability rule
you have. Then section 6, which is the measured argument for the short window:
without it a three-minute outage holds an alert lit for most of an hour.
"""
import math

# --- The SLO -----------------------------------------------------------------

WINDOW_DAYS = 28
TARGET = 0.999                      # 99.9%
LATENCY_THRESHOLD_MS = 300          # the SLI's "good" boundary
REQUESTS_PER_MINUTE = 6000          # 100 req/s, constant, to keep it checkable

WINDOW_MINUTES = WINDOW_DAYS * 24 * 60
BUDGET_MINUTES = (1 - TARGET) * WINDOW_MINUTES
BUDGET_EVENTS = (1 - TARGET) * WINDOW_MINUTES * REQUESTS_PER_MINUTE

# --- The rules ---------------------------------------------------------------
# (long window, short window, burn rate, action). Straight from the SRE
# workbook, and re-derived in section 2 rather than taken on faith.
RULES = [
    ("1h", 60, "5m", 5, 14.4, "page"),
    ("6h", 360, "30m", 30, 6.0, "page"),
    ("3d", 4320, "6h", 360, 1.0, "ticket"),
]

# --- The simulated timeline --------------------------------------------------
SIM_MINUTES = 6 * 24 * 60           # 6 days: enough for a full 3d window
INCIDENT_START = 4 * 24 * 60        # day 4, so every window is warm

# Baseline behaviour. tau is chosen so the baseline p99 is 150ms.
BASELINE_TAU_MS = 150 / math.log(100)
BASELINE_ERROR_RATIO = 0.0002       # 0.02%: a healthy service is not perfect
EVAL_INTERVAL = 1                   # Prometheus evaluates every minute here


def p99_of(tau_ms):
    """Exponential: P(X > t) = exp(-t/tau), so the p99 is tau*ln(100)."""
    return tau_ms * math.log(100)


def fraction_slower_than(tau_ms, threshold_ms):
    return math.exp(-threshold_ms / tau_ms)


def tau_for_p99(p99_ms):
    return p99_ms / math.log(100)


# ---------------------------------------------------------------------------
# Incident shapes. Each returns, for a given minute, the availability error
# ratio and the latency tau in force during that minute.
# ---------------------------------------------------------------------------

class Shape:
    def __init__(self, name, description, duration, error_ratio=None, p99_ms=None):
        self.name = name
        self.description = description
        self.duration = duration
        self.error_ratio = error_ratio
        self.p99_ms = p99_ms

    def active(self, minute):
        return INCIDENT_START <= minute < INCIDENT_START + self.duration

    def error_ratio_at(self, minute):
        if self.active(minute) and self.error_ratio is not None:
            return self.error_ratio
        return BASELINE_ERROR_RATIO

    def tau_at(self, minute):
        if self.active(minute) and self.p99_ms is not None:
            return tau_for_p99(self.p99_ms)
        return BASELINE_TAU_MS


SHAPES = [
    Shape("A", "total outage, 3 minutes", 3, error_ratio=1.0),
    Shape("B", "8% error rate, 4 hours", 240, error_ratio=0.08),
    Shape("C", "latency p99 300ms -> 700ms, 45 min, zero errors", 45, p99_ms=700),
]


# ---------------------------------------------------------------------------
# The simulation: per-minute counts, then rolling windows over them.
# ---------------------------------------------------------------------------

def simulate(shape):
    """Returns per-minute bad-event counts for both SLIs, plus the p99 series."""
    availability_bad = []
    latency_bad = []
    p99_series = []
    for minute in range(SIM_MINUTES):
        errors = shape.error_ratio_at(minute) * REQUESTS_PER_MINUTE
        tau = shape.tau_at(minute)
        # A request is "bad" for the latency SLI if it is slower than the
        # threshold. Errors are bad for the availability SLI. The two SLIs are
        # deliberately independent: shape C has zero errors.
        slow = fraction_slower_than(tau, LATENCY_THRESHOLD_MS) * REQUESTS_PER_MINUTE
        availability_bad.append(errors)
        latency_bad.append(slow)
        p99_series.append(p99_of(tau))
    return availability_bad, latency_bad, p99_series


def prefix_sums(values):
    out = [0.0]
    total = 0.0
    for v in values:
        total += v
        out.append(total)
    return out


def burn_rate(prefix, minute, window):
    """Burn rate over the trailing `window` minutes ending at `minute`.

    burn = (bad / valid) / (1 - target). Rate 1 exhausts the budget exactly at
    the end of the SLO window; rate n exhausts it in window/n.
    """
    start = minute - window + 1
    if start < 0:
        return 0.0
    bad = prefix[minute + 1] - prefix[start]
    valid = window * REQUESTS_PER_MINUTE
    return (bad / valid) / (1 - TARGET)


def evaluate(prefix, use_short_window=True):
    """Which minutes each rule is firing on. Returns {rule_name: [minutes]}."""
    firing = {}
    for long_label, long_win, short_label, short_win, factor, _action in RULES:
        minutes = []
        for minute in range(long_win, SIM_MINUTES, EVAL_INTERVAL):
            long_burn = burn_rate(prefix, minute, long_win)
            if long_burn <= factor:
                continue
            if use_short_window:
                short_burn = burn_rate(prefix, minute, short_win)
                if short_burn <= factor:
                    continue
            minutes.append(minute)
        firing["%s/%s @ %sx" % (long_label, short_label, fmt(factor))] = minutes
    return firing


def naive_latency_rule(p99_series, threshold_ms=500, for_minutes=5):
    """`p99 > 500ms for 5m`. The rule everybody has, on the metric everybody has."""
    firing = []
    streak = 0
    for minute, p99 in enumerate(p99_series):
        streak = streak + 1 if p99 > threshold_ms else 0
        if streak >= for_minutes:
            firing.append(minute)
    return firing


def fmt(value):
    return ("%g" % value)


def summarise(minutes, incident_start, incident_end):
    if not minutes:
        return None
    first = min(minutes)
    last = max(minutes)
    return {
        "first": first,
        "detect": first - incident_start,
        "lit_minutes": len(minutes),
        "lit_after_end": max(0, last - incident_end + 1) if last >= incident_end else 0,
    }


def main():
    print("Layer 6 Topic 6 - error budgets and multi-window burn-rate alerting")
    print("=" * 78)

    # -----------------------------------------------------------------------
    print()
    print("1. The budget, derived rather than quoted")
    print("-" * 60)
    print("  window            %d days = %d x 24 x 60 = %s minutes"
          % (WINDOW_DAYS, WINDOW_DAYS, f"{WINDOW_MINUTES:,}"))
    print("  target            %.3f%%" % (100 * TARGET))
    print("  budget            (1 - %.3f) x %s = %.2f minutes"
          % (TARGET, f"{WINDOW_MINUTES:,}", BUDGET_MINUTES))
    print("  budget in events  %.2f min x %s req/min = %s bad requests"
          % (BUDGET_MINUTES, f"{REQUESTS_PER_MINUTE:,}", f"{BUDGET_EVENTS:,.0f}"))
    print()
    print("  Forty minutes. Not 'three nines', which sounds generous -- forty")
    print("  minutes, which sounds like one bad deploy, because it is one bad")
    print("  deploy.")

    # -----------------------------------------------------------------------
    print()
    print("2. The burn-rate table, re-derived from that one number")
    print("-" * 60)
    print("  burn rate n exhausts the budget in window/n, and consumes")
    print("  (short_window x n / budget_minutes) of the month while it lasts.")
    print()
    print("  %-6s %-7s %-7s %-16s %-16s %s"
          % ("long", "short", "rate", "exhausts budget", "spent in long win", "action"))
    print("  %-6s %-7s %-7s %-16s %-16s %s"
          % ("-" * 6, "-" * 7, "-" * 7, "-" * 16, "-" * 16, "-" * 6))
    for long_label, long_win, short_label, _short_win, factor, action in RULES:
        exhausts_minutes = WINDOW_MINUTES / factor
        spent_pct = 100 * long_win * factor / WINDOW_MINUTES
        print("  %-6s %-7s %-7s %-16s %-16s %s"
              % (long_label, short_label, fmt(factor) + "x",
                 "%.0f min (%.1f d)" % (exhausts_minutes, exhausts_minutes / 1440),
                 "%.1f%%" % spent_pct, action))
    print()
    print("  Check row 1 by hand: %s / 14.4 = %.0f minutes = %.1f days to burn"
          % (f"{WINDOW_MINUTES:,}", WINDOW_MINUTES / 14.4, WINDOW_MINUTES / 14.4 / 1440))
    print("  the whole month, and 60 x 14.4 / %s = %.1f%% of it spent in the"
          % (f"{WINDOW_MINUTES:,}", 100 * 60 * 14.4 / WINDOW_MINUTES))
    print("  hour the rule looks at. None of these are magic constants, which")
    print("  is what lets you rebuild the table for a window that is not 28 days.")

    # -----------------------------------------------------------------------
    print()
    print("3. The SLI threshold, and the most common way to pick it wrong")
    print("-" * 60)
    baseline_p99 = p99_of(BASELINE_TAU_MS)
    baseline_slow = fraction_slower_than(BASELINE_TAU_MS, LATENCY_THRESHOLD_MS)
    at_p99_threshold = fraction_slower_than(BASELINE_TAU_MS, baseline_p99)
    print("  baseline latency model   exponential, tau = %.1f ms" % BASELINE_TAU_MS)
    print("  baseline p99             %.0f ms" % baseline_p99)
    print()
    print("  SLI = fraction of requests under %d ms" % LATENCY_THRESHOLD_MS)
    print("    baseline bad ratio     %.4f%%   burn rate %.2f  -> compliant"
          % (100 * baseline_slow, baseline_slow / (1 - TARGET)))
    print()
    print("  If instead you set the threshold at today's p99 (%d ms):" % baseline_p99)
    print("    baseline bad ratio     %.2f%%     burn rate %.0f    -> already failing"
          % (100 * at_p99_threshold, at_p99_threshold / (1 - TARGET)))
    print()
    print("  That is not a subtle trap: 1% of requests exceed the p99 BY")
    print("  DEFINITION, and 1% is ten times a 99.9% budget. Setting the SLO")
    print("  threshold to your current p99 guarantees permanent breach on day")
    print("  one. Pick the threshold from what users tolerate, then check what")
    print("  fraction of traffic is already outside it.")

    # -----------------------------------------------------------------------
    print()
    print("4. The three shapes, as the SLIs see them")
    print("-" * 60)
    print("  %-4s %-44s %-12s %-12s"
          % ("", "shape", "err ratio", "p99"))
    for shape in SHAPES:
        tau = shape.tau_at(INCIDENT_START)
        print("  %-4s %-44s %-12s %-12s"
              % (shape.name, shape.description,
                 "%.2f%%" % (100 * shape.error_ratio_at(INCIDENT_START)),
                 "%.0f ms" % p99_of(tau)))
    print()
    degraded_tau = tau_for_p99(700)
    print("  Shape C in the SLI's own terms: a p99 of 700 ms means tau = %.0f ms,"
          % degraded_tau)
    print("  so the fraction of requests over %d ms goes from %.4f%% to %.2f%%."
          % (LATENCY_THRESHOLD_MS, 100 * baseline_slow,
             100 * fraction_slower_than(degraded_tau, LATENCY_THRESHOLD_MS)))
    print("  That is a burn rate of %.0fx with zero errors and a completely"
          % (fraction_slower_than(degraded_tau, LATENCY_THRESHOLD_MS) / (1 - TARGET)))
    print("  green availability dashboard.")

    # -----------------------------------------------------------------------
    print()
    print("5. Replay: which rules fire, how fast, and what each shape costs")
    print("-" * 60)

    results = []
    for shape in SHAPES:
        availability_bad, latency_bad, p99_series = simulate(shape)
        end = INCIDENT_START + shape.duration

        # Budget consumed by the incident itself, over the baseline.
        av_incident = sum(availability_bad[INCIDENT_START:end])
        lat_incident = sum(latency_bad[INCIDENT_START:end])
        av_baseline = BASELINE_ERROR_RATIO * REQUESTS_PER_MINUTE * shape.duration
        lat_baseline = (fraction_slower_than(BASELINE_TAU_MS, LATENCY_THRESHOLD_MS)
                        * REQUESTS_PER_MINUTE * shape.duration)

        av_prefix = prefix_sums(availability_bad)
        lat_prefix = prefix_sums(latency_bad)

        av_firing = evaluate(av_prefix)
        lat_firing = evaluate(lat_prefix)
        naive = naive_latency_rule(p99_series)

        results.append({
            "shape": shape,
            "av_budget": 100 * (av_incident - av_baseline) / BUDGET_EVENTS,
            "lat_budget": 100 * (lat_incident - lat_baseline) / BUDGET_EVENTS,
            "av_firing": av_firing,
            "lat_firing": lat_firing,
            "naive": naive,
            "end": end,
        })

        print()
        print("  Shape %s: %s" % (shape.name, shape.description))
        print("    budget spent    availability %.2f%%   latency %.2f%%"
              % (100 * (av_incident - av_baseline) / BUDGET_EVENTS,
                 100 * (lat_incident - lat_baseline) / BUDGET_EVENTS))
        print("    %-24s %-10s %-14s %s"
              % ("rule", "fired?", "after onset", "stayed lit after recovery"))
        for sli_name, firing in (("availability", av_firing), ("latency", lat_firing)):
            for rule_name, minutes in firing.items():
                info = summarise(minutes, INCIDENT_START, end)
                if info is None:
                    print("    %-24s %-10s %-14s %s"
                          % ("%s %s" % (sli_name[:4], rule_name), "no", "-", "-"))
                else:
                    print("    %-24s %-10s %-14s %s"
                          % ("%s %s" % (sli_name[:4], rule_name), "YES",
                             "%d min" % info["detect"],
                             "%d min" % info["lit_after_end"]))
        naive_info = summarise(naive, INCIDENT_START, end)
        if naive_info is None:
            print("    %-24s %-10s %-14s %s"
                  % ("naive p99>500ms for 5m", "no", "-", "-"))
        else:
            print("    %-24s %-10s %-14s %s"
                  % ("naive p99>500ms for 5m", "YES",
                     "%d min" % naive_info["detect"],
                     "%d min" % naive_info["lit_after_end"]))

    # -----------------------------------------------------------------------
    print()
    print("6. The short window: what it is for, measured")
    print("-" * 60)
    print("  Same shapes, same long windows, with and without the short-window")
    print("  condition. The column that matters is the last one.")
    print()
    print("  %-6s %-24s %-20s %-20s"
          % ("shape", "rule", "lit after, no short", "lit after, with short"))
    print("  %-6s %-24s %-20s %-20s"
          % ("-" * 6, "-" * 24, "-" * 20, "-" * 20))
    for result in results:
        shape = result["shape"]
        bad = simulate(shape)[0 if shape.error_ratio is not None else 1]
        prefix = prefix_sums(bad)
        with_short = evaluate(prefix, use_short_window=True)
        without_short = evaluate(prefix, use_short_window=False)
        for rule_name in with_short:
            a = summarise(without_short[rule_name], INCIDENT_START, result["end"])
            b = summarise(with_short[rule_name], INCIDENT_START, result["end"])
            if a is None and b is None:
                continue
            print("  %-6s %-24s %-20s %-20s"
                  % (shape.name, rule_name,
                     "%d min" % (a["lit_after_end"] if a else 0),
                     "%d min" % (b["lit_after_end"] if b else 0)))
    print()
    print("  Without the short window an alert keeps firing until the LONG")
    print("  window rolls off: a 3-minute outage can hold a 1-hour rule lit for")
    print("  most of an hour, and a 6-hour rule for most of six. The short")
    print("  window is what makes recovery clear the page, and dropping it is")
    print("  why people conclude that burn-rate alerting is noisy.")

    # -----------------------------------------------------------------------
    print()
    print("7. Budget bookkeeping for all three shapes together")
    print("-" * 60)
    total = sum(r["av_budget"] + r["lat_budget"] for r in results)
    print("  %-46s %s" % ("budget for %.1f%% over %d days"
                          % (100 * TARGET, WINDOW_DAYS),
                          "%.2f minutes" % BUDGET_MINUTES))
    for r in results:
        spent = r["av_budget"] + r["lat_budget"]
        print("  %-46s %.2f%%  (%.2f min equivalent)"
              % ("shape %s: %s" % (r["shape"].name, r["shape"].description),
                 spent, BUDGET_MINUTES * spent / 100))
    print("  %-46s %.2f%%" % ("total spent", total))
    print("  %-46s %.2f%%" % ("remaining", 100 - total))
    print()
    a_pages = [name for name, minutes in results[0]["av_firing"].items() if minutes]
    print("  Shape A spends %.1f%% of a month's budget in three minutes, and on"
          % results[0]["av_budget"])
    if a_pages:
        print("  these rules it does page: %s." % ", ".join(a_pages))
        print("  Whether that is right is a design decision, and it is now a design")
        print("  decision with a number attached -- thirteen more outages exactly")
        print("  like it and the month's budget is gone. If you would rather not be")
        print("  woken for three minutes, the lever is the rule's `for:` duration,")
        print("  not the burn rate: the budget arithmetic is not the thing you")
        print("  disagree with, the paging threshold is.")
    else:
        print("  and pages nobody. That is a design decision too, and the same")
        print("  arithmetic is what makes it arguable rather than a matter of taste.")
    print()
    print("  Two honest limits of this simulator, both of which make it optimistic:")
    print("    * it evaluates every minute with no `for:` duration, so detection")
    print("      times here are floors. Add your evaluation interval and `for:`.")
    print("    * traffic is constant at %s req/min. Real traffic has a daily"
          % f"{REQUESTS_PER_MINUTE:,}")
    print("      shape, and the same outage at 04:00 spends far less budget than")
    print("      at peak -- which is a feature of an event-ratio SLI, not a bug.")
    print()
    print("  That is the whole point of the mechanism. 'Was that bad?' stops")
    print("  being a status contest and becomes a subtraction.")


if __name__ == "__main__":
    main()
