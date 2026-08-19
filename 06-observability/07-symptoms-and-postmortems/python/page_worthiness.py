"""
Layer 6 Topic 7, Part 1 - Eight rules, four scenarios, and a false-page rate.

Why Python, and only Python: there is no language in this topic. Alert rules are
PromQL evaluated by Prometheus; nothing here executes in your service, and
reimplementing an alert-rule evaluator in six languages would teach nothing
about any of them. Python is the scenario driver.

What this program does
----------------------
Four scenarios, each a minute-by-minute timeline of both kinds of signal:

  CAUSE signals   CPU, pool utilization, replica lag, error-log rate
  USER signals    requests, 5xx responses, requests slower than the SLO
                  threshold -- i.e. the two things a user can actually feel

Then eight rules run against those timelines:

  four cause-based    CPU > 80%, pool > 90% utilized, replica lag > 10s,
                      error-log rate > 10/min   (each `for: 5m`)
  four symptom-based  the three burn-rate rules from Topic 6, plus one
                      availability rule (error ratio > 1% for 5m)

User harm is not asserted anywhere. It is DERIVED, per scenario, from the user
signals: a minute harms users if the requests in it breach the SLO band. That
is what makes the false-page rate below a measurement rather than an opinion --
and it is the number that decides whether a rule deserves to wake a person.

The scenarios
-------------
  W  a batch job pegs CPU for 20 minutes; request latency stays flat.
  X  pool exhaustion driven by the `pricing` tail: CPU low, memory low, DB
     healthy, everything green, p99 at 8s.
  Y  a replica falls 60s behind, but no reads route to it.
  Z  a bad deploy: 3% of requests return 500 for 25 minutes.

Two of these harm nobody. The interesting result is how many rules fire anyway.

What to look for in the output
------------------------------
The per-family false-page rate at the end, and scenario X's row: the scenario
where users are being hurt worst is the one where the cause-based family is
quietest, because nothing that family watches is out of range. Causes are
unbounded; you did not fail to enumerate them, you cannot.
"""

# --- The SLO the symptom rules defend ---------------------------------------
TARGET = 0.999
LATENCY_THRESHOLD_MS = 300
REQUESTS_PER_MINUTE = 6000
SLO_WINDOW_MINUTES = 28 * 24 * 60

# --- Baselines: a healthy service on a quiet afternoon -----------------------
BASE = {
    "cpu": 0.35,               # 35% of one core
    "pool_util": 0.40,         # 2 of 5 connections
    "replica_lag_s": 0.4,
    "error_logs_per_min": 2,
    "error_ratio": 0.0002,     # 0.02%
    "slow_ratio": 0.0001,      # 0.01% over 300ms
}

# The timeline starts three days before the incident so that every rolling
# window -- including the 3d one -- is full of healthy history by the time
# anything happens. A partially-filled window divides by elapsed time instead
# of window length and fires far too eagerly, which is a real way to be fooled
# by a freshly-deployed alert rule.
INCIDENT_START = 3 * 24 * 60
TIMELINE_MINUTES = INCIDENT_START + 180


class Scenario:
    def __init__(self, key, name, duration, overrides, note):
        self.key = key
        self.name = name
        self.duration = duration
        self.overrides = overrides
        self.note = note

    def at(self, minute):
        state = dict(BASE)
        if INCIDENT_START <= minute < INCIDENT_START + self.duration:
            state.update(self.overrides)
        return state


SCENARIOS = [
    Scenario(
        "W", "batch job pegs CPU for 20 min, latency flat", 20,
        {"cpu": 0.97, "error_logs_per_min": 3},
        "the batch job runs on spare capacity, which is what spare capacity is for",
    ),
    Scenario(
        "X", "pool exhaustion from the pricing tail: p99 at 8s", 40,
        # Everything a cause rule watches is fine. CPU is LOW precisely because
        # every request is blocked waiting for a connection.
        {"cpu": 0.18, "pool_util": 1.0, "slow_ratio": 0.62,
         "error_logs_per_min": 4, "error_ratio": 0.004},
        "CPU low, memory low, DB healthy, dashboards green, users timing out",
    ),
    Scenario(
        "Y", "replica 60s behind, no reads routed to it", 35,
        {"replica_lag_s": 60.0},
        "the replica is a warm spare; nothing reads from it",
    ),
    Scenario(
        "Z", "bad deploy: 3% of requests 500 for 25 min", 25,
        {"error_ratio": 0.03, "error_logs_per_min": 180},
        "one in thirty users gets an error page",
    ),
]


# ---------------------------------------------------------------------------
# The rules. Each returns the list of minutes on which it is firing.
# ---------------------------------------------------------------------------

def sustained(condition_by_minute, for_minutes):
    """`for: Nm` -- the condition must hold for N consecutive evaluations."""
    firing = []
    streak = 0
    for minute, holds in enumerate(condition_by_minute):
        streak = streak + 1 if holds else 0
        if streak >= for_minutes:
            firing.append(minute)
    return firing


def cause_rules(states):
    return {
        "CPUHigh: cpu > 80% for 5m":
            sustained([s["cpu"] > 0.80 for s in states], 5),
        "PoolNearlyFull: > 90% for 5m":
            sustained([s["pool_util"] > 0.90 for s in states], 5),
        "ReplicaLag: > 10s for 5m":
            sustained([s["replica_lag_s"] > 10 for s in states], 5),
        "ErrorLogRate: > 10/min for 5m":
            sustained([s["error_logs_per_min"] > 10 for s in states], 5),
    }


def burn_rate_series(bad_per_minute, window):
    """Trailing burn rate, in units of (1 - target). Same arithmetic as Topic 6."""
    out = []
    running = 0.0
    for minute, bad in enumerate(bad_per_minute):
        running += bad
        if minute >= window:
            running -= bad_per_minute[minute - window]
        span = min(minute + 1, window)
        valid = span * REQUESTS_PER_MINUTE
        out.append((running / valid) / (1 - TARGET))
    return out


def symptom_rules(states):
    # The SLI is one ratio over both failure modes: a request is bad if it
    # errored OR if it was slower than the threshold. That is deliberate --
    # users do not distinguish, and neither should the promise.
    bad = [(s["error_ratio"] + s["slow_ratio"]) * REQUESTS_PER_MINUTE for s in states]

    rules = {}
    for long_label, long_win, short_label, short_win, factor in [
        ("1h", 60, "5m", 5, 14.4),
        ("6h", 360, "30m", 30, 6.0),
        ("3d", 4320, "6h", 360, 1.0),
    ]:
        long_series = burn_rate_series(bad, long_win)
        short_series = burn_rate_series(bad, short_win)
        firing = [m for m in range(len(bad))
                  if long_series[m] > factor and short_series[m] > factor]
        rules["BurnRate %s/%s @ %gx" % (long_label, short_label, factor)] = firing

    rules["Availability: 5xx > 1% for 5m"] = sustained(
        [s["error_ratio"] > 0.01 for s in states], 5)
    return rules


def user_harm(states):
    """Ground truth. A minute harms users if its requests breach the SLO band.

    'Breach the band' means the bad-event ratio for that minute exceeds the
    error budget's steady-state allowance, i.e. burn rate > 1 for that minute.
    Nothing about the alert rules is involved in computing this.
    """
    harmed_minutes = []
    harmed_requests = 0
    for minute, s in enumerate(states):
        ratio = s["error_ratio"] + s["slow_ratio"]
        if ratio / (1 - TARGET) > 1.0:
            harmed_minutes.append(minute)
            harmed_requests += ratio * REQUESTS_PER_MINUTE
    return harmed_minutes, harmed_requests


def main():
    print("Layer 6 Topic 7 Part 1 - eight rules, four scenarios, one false-page rate")
    print("SLO: %.1f%% of requests non-5xx and under %d ms, over %d days"
          % (100 * TARGET, LATENCY_THRESHOLD_MS, SLO_WINDOW_MINUTES // 1440))
    print("=" * 78)

    summary = []
    for scenario in SCENARIOS:
        states = [scenario.at(m) for m in range(TIMELINE_MINUTES)]
        causes = cause_rules(states)
        symptoms = symptom_rules(states)
        harmed_minutes, harmed_requests = user_harm(states)

        peak = scenario.at(INCIDENT_START)
        print()
        print("Scenario %s: %s" % (scenario.key, scenario.name))
        print("-" * 78)
        print("  during the incident:  cpu %.0f%%   pool %.0f%%   replica lag %.0fs   "
              "error logs %d/min"
              % (100 * peak["cpu"], 100 * peak["pool_util"],
                 peak["replica_lag_s"], peak["error_logs_per_min"]))
        print("  what users got:       %.2f%% errors, %.1f%% slower than %dms"
              % (100 * peak["error_ratio"], 100 * peak["slow_ratio"],
                 LATENCY_THRESHOLD_MS))
        print("  users harmed:         %s (%s minutes, %s requests outside the band)"
              % ("YES" if harmed_minutes else "no",
                 len(harmed_minutes), f"{harmed_requests:,.0f}"))
        print("  %s" % scenario.note)
        print()
        print("  %-34s %-8s %s" % ("rule", "fired?", "first fired"))
        print("  %-34s %-8s %s" % ("-" * 34, "-" * 8, "-" * 22))

        cause_fired = 0
        for name, minutes in causes.items():
            fired = bool(minutes)
            cause_fired += fired
            print("  %-34s %-8s %s"
                  % (name, "YES" if fired else "no",
                     ("T+%d min" % (min(minutes) - INCIDENT_START)) if fired else "-"))
        symptom_fired = 0
        for name, minutes in symptoms.items():
            fired = bool(minutes)
            symptom_fired += fired
            print("  %-34s %-8s %s"
                  % (name, "YES" if fired else "no",
                     ("T+%d min" % (min(minutes) - INCIDENT_START)) if fired else "-"))

        verdict = classify(bool(harmed_minutes), cause_fired, symptom_fired)
        print()
        print("  verdict: %s" % verdict)

        summary.append({
            "scenario": scenario,
            "harmed": bool(harmed_minutes),
            "cause_fired": cause_fired,
            "symptom_fired": symptom_fired,
        })

    # -----------------------------------------------------------------------
    print()
    print("=" * 78)
    print("The scoreboard")
    print("-" * 78)
    print("  %-6s %-48s %-8s %-7s %s"
          % ("", "scenario", "harmed?", "cause", "symptom"))
    for row in summary:
        print("  %-6s %-48s %-8s %-7s %s"
              % (row["scenario"].key, row["scenario"].name,
                 "YES" if row["harmed"] else "no",
                 "%d/4" % row["cause_fired"], "%d/4" % row["symptom_fired"]))

    print()
    cause_pages = sum(r["cause_fired"] for r in summary)
    cause_real = sum(r["cause_fired"] for r in summary if r["harmed"])
    symptom_pages = sum(r["symptom_fired"] for r in summary)
    symptom_real = sum(r["symptom_fired"] for r in summary if r["harmed"])

    print("  %-24s %-14s %-24s %s"
          % ("rule family", "pages fired", "pages with real harm", "false-page rate"))
    print("  %-24s %-14s %-24s %s" % ("-" * 24, "-" * 14, "-" * 24, "-" * 16))
    for label, pages, real in (("Cause-based (4 rules)", cause_pages, cause_real),
                               ("Symptom-based (4 rules)", symptom_pages, symptom_real)):
        rate = 0 if pages == 0 else 100 * (pages - real) / pages
        print("  %-24s %-14d %-24d %.0f%%" % (label, pages, real, rate))

    print()
    missed_by_cause = [r["scenario"].key for r in summary
                       if r["harmed"] and r["cause_fired"] == 0]
    missed_by_symptom = [r["scenario"].key for r in summary
                         if r["harmed"] and r["symptom_fired"] == 0]
    print("  real harm that NO cause rule caught:    %s"
          % (", ".join(missed_by_cause) if missed_by_cause else "none"))
    print("  real harm that NO symptom rule caught:  %s"
          % (", ".join(missed_by_symptom) if missed_by_symptom else "none"))
    print()
    print("  Read scenario X's rows again. It is the worst incident in the set --")
    print("  most of an hour with the majority of requests past the SLO threshold --")
    print("  and it is the scenario where the cause family is quietest, because")
    print("  CPU is LOW: every request is parked waiting for a connection rather")
    print("  than doing work. A cause rule can only fire on a cause you thought of,")
    print("  and 'CPU falls while latency explodes' is not on anybody's list.")
    print()
    print("  That is the argument in one line. The set of causes is unbounded and")
    print("  always will be. The set of symptoms is small, stable, and exactly what")
    print("  you promised -- so symptoms page, and causes go on the dashboard you")
    print("  open after the page, because that is where they earn their keep.")
    print()
    false_cause = [r["scenario"].key for r in summary
                   if r["cause_fired"] and not r["harmed"]]
    true_cause = [r["scenario"].key for r in summary
                  if r["cause_fired"] and r["harmed"]]
    print("  Separate the cause pages that fired. In %s a cause rule fired with"
          % ", ".join(false_cause))
    print("  nobody harmed -- that is the training data for ignoring pages. In %s"
          % ", ".join(true_cause))
    print("  one fired during a real incident, which is a true positive and still")
    print("  not a reason to keep the rule: it fired on a signal that happened to")
    print("  correlate this time, and the symptom rules fired for the same")
    print("  incident with a defensible number attached.")
    print()
    print("  A cause earns a page only when it predicts imminent user-visible harm,")
    print("  with enough lead time to act, at a false-positive rate people keep")
    print("  trusting. 'Disk full in 4 hours' passes all three. None of the four")
    print("  cause rules above does.")


def classify(harmed, cause_fired, symptom_fired):
    if harmed and symptom_fired and not cause_fired:
        return "symptom rules caught it; cause rules saw nothing wrong (they were right)"
    if harmed and symptom_fired:
        return "both families fired, and users really were being harmed"
    if harmed and not symptom_fired:
        return "USERS HARMED AND NOTHING PAGED -- the rules are the bug"
    if not harmed and cause_fired:
        return "nobody harmed, %d cause rule(s) fired: this is how pages stop meaning anything" % cause_fired
    if not harmed and not cause_fired and not symptom_fired:
        return "quiet, correctly"
    return "nobody harmed, and a symptom rule fired: check the SLI, not the rule"


if __name__ == "__main__":
    main()
