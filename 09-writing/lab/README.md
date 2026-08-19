# Layer 9 · The shared workspace

The other layers share a `docker compose` stack. This one shares a **filing
system and a scoring rule**, because the artifacts here are documents and the
thing that makes them work is not a runtime — it is that each one gets sent to a
named human by a date, and that what came back gets written down.

Topic READMEs reference the paths and column names on this page rather than
restating them. If you rename a directory or a column, rename it here first.

---

## Layout

```
09-writing/
  templates/        # the fenced templates from the topics, copied into real files
    design-doc.md       # defined in Topic 1
    postmortem.md       # defined in Topic 4
    pr-description.md   # defined in Topic 6
  artifacts/        # what you actually write, one folder per topic
    01-design-doc/
    02-alternatives/    # holds versions A, B and C — see Topic 2
    03-rfc/
    04-postmortem/
    05-tradeoff/
    06-commits/
    07-posts/
  log.md            # the spine (below)
```

Create it once:

```bash
cd 09-writing
mkdir -p templates artifacts/{01-design-doc,02-alternatives,03-rfc,04-postmortem,05-tradeoff,06-commits,07-posts}
touch log.md
```

Nothing here is throwaway. `templates/` is the part you keep for the rest of
your career; the topics are how you earn the right to trust each template.

---

## `log.md` — the spine

One row per artifact. Fill the prediction columns **before** you send.

| # | Artifact | Sent to | Predicted top objection | Actual top objection | Specific disagreement? |
|---|---|---|---|---|---|
| | | | | | |

Column semantics, because the last one is the only score that matters:

- **Sent to** — a person's name. A channel is not a name. If this cell says
  `#eng`, the row is void; nobody owns a reply to a channel.
- **Predicted top objection** — written before sending, in one sentence, naming
  the section it will land in.
- **Specific disagreement?** — `yes` only if the reader attacked a *tradeoff by
  name*. "What did you mean by X" is a miss. Silence is a miss, and usually the
  most informative one, because it means the doc contained nothing to argue with
  or reached nobody who had to live with it.

Topic 7 adds a second table to the same file:

| Month | Post | The scarce thing in it | Where it came from |
|---|---|---|---|
| | | | |

---

## The two rules that apply to every artifact in this layer

**1. Never write a number you did not measure.** Inherited from the rest of the
lab, and it bites hardest in a postmortem, which is read by people who make
resourcing decisions from it. If you do not have the number, write
`unknown — see Detection Gaps`. An honest `unknown` is not a weakness in the
document; it is a finding, and it usually converts directly into the best action
item in the whole doc. The corollary: **every number in an artifact is either
derived on the page or carries its source** — the dashboard query, the log
range, the command you ran.

Worked examples in the topic READMEs use placeholders (`<your number>`,
`<window>`) for exactly this reason. They are showing you sentence *shape*. Fill
them from your own system or delete the sentence.

**2. Make at least one claim specific enough to be wrong.** Every topic here is
the same move in a different venue, and this is the move.

---

## The sanitisation gate

The mechanical half of this checklist is scripted at
[`tools/sanitise_gate.py`](tools/sanitise_gate.py) — run
`python3 lab/tools/sanitise_gate.py` from the `09-writing` root. It prints the
items it *cannot* check at the end of every run, because those are the ones that
matter.

Applies to anything leaving your organisation — the Topic 7 post, and any
Topic 4 postmortem you publish. Run it **before** you write, not after; it is
much harder to launder a draft than to write a clean one.

- No customer data, and nothing that identifies a specific customer's traffic
  pattern.
- No internal hostnames, endpoints, service names that map to internal systems,
  or ticket IDs.
- No employee names. This includes the "who was on call" line.
- No absolute revenue figures. Ratios and percentages instead of absolutes;
  relative latencies where the absolutes are sensitive.
- Employer sign-off in writing before publishing anything derived from a
  production incident. Every company draws this line somewhere different and you
  want to find the line before publication rather than after.

---

## What this layer borrows from other layers

- **Topic 1 and Topic 4** are about your live FastAPI/Postgres latency problem.
  Where the postmortem hits a question your current instrumentation cannot
  answer, go get the evidence with the stack in
  [`06-observability/lab/`](../../06-observability/lab/README.md) — its planted
  defects are a catalogue of the usual suspects in exactly this kind of
  incident.
- **Topic 6** runs on your own repositories: this lab's, and the production
  service's.
- **Topic 7's** best raw material is the rest of this repo — see the scarcity
  ranking in that topic.
