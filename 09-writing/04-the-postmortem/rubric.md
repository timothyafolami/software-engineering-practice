# Topic 4 rubric — score the postmortem before it is circulated

Eight items from [`README.md`](README.md). Five of them are mechanical and run
from [`python/postmortem_check.py`](python/postmortem_check.py); the three that
are not are the three that matter most, which is the usual arrangement.

| # | Check | How to test it | Score |
|---|---|---|---|
| 1 | A reader who was not there can state, from the Summary alone, what users experienced | Give them the Summary only. Ask what happened. Compare | |
| 2 | Every number cites where it came from | `python3 python/postmortem_check.py <file>` | |
| 3 | The timeline starts at the change that made the incident possible, not at the alert | Read row 1. Is it a deploy, a config change, a dependency change? | |
| 4 | Three or more contributing factors, at least one a **default nobody chose** | Count them. Then find the one nobody decided | |
| 5 | The detection-gap section explains what you believed first and why it was reasonable | If the first hypothesis looks stupid, hindsight removed the lesson | |
| 6 | Zero counterfactuals | script — greps "should have", "if only", "forgot to", … | |
| 7 | Every action changes a system | script flags the weak ones; then cover the doc-shaped actions with your hand | |
| 8 | No sentence names an individual as a cause | script flags person-shaped subjects in factors and actions | |

## The primary output of pass 1 is not a score

It is the count of cells you had to mark `unknown`. The script prints it first.
Each `unknown` is a detection gap, each detection gap becomes an action, and that
list is the honest version of "instrument the service" — questions this document
could not answer, rather than whatever was easy to graph.

If you have zero, check whether you reconstructed the timeline from memory.
Memory is confident and wrong.

## Item 4 — finding the default nobody chose

Read every line of the code path and ask, for each behaviour: *who decided
this?* The ones with no answer are the factors worth writing. A library default
(`requests` applies no timeout unless you pass one), a pool size inherited from
an example in a tutorial, a retention period that came with the plan. These are
the factors that generate actions changing a class of behaviour rather than one
line, and they are invisible to anyone reading the diff, because they are not in
the diff.

## Item 5 — the test people fail without noticing

The section is not "what we thought at first, which was silly". It is: *given
the graphs that existed, what was the most available explanation, and what
signal's absence made it the most available one?* The finding is the missing
signal, not the wrong guess. A postmortem that mocks its own first hypothesis
has removed the only reusable thing in it.

## Mechanical checks

```sh
# from 09-writing/04-the-postmortem
python3 python/postmortem_check.py worked-example.md          # calibrate on a clean one
python3 python/postmortem_check.py                            # then your own draft

# the README's greps, if you prefer them raw
cd ../artifacts/04-postmortem
grep -nEi "should have|if only|failed to|forgot to|could have" latency-incident.md
grep -nc "unknown" latency-incident.md
```

Then run the sanitisation gate **before** writing any version that leaves the
building — `python3 lab/tools/sanitise_gate.py` from the `09-writing` root — and
circulate to two named people, one of whom was involved. Log the row in
[`../log.md`](../log.md).
