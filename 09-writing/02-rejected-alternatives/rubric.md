# Topic 2 rubric — score version B before you circulate it

Six items from [`README.md`](README.md), as checks. Items 1–3 have a script;
items 4–6 are the ones that need you.

| # | Check | How to test it | Score |
|---|---|---|---|
| 1 | Every rejection contains a fact about *your* system | `sh tools/flip-check.sh <file>` — flags blocks with no number, config value or placeholder | |
| 2 | Every alternative has a flip condition, written as something observable | same script | |
| 3 | "Do nothing" is present, rejected for a reason other than "the status quo is bad" | script checks presence; the *reason* is yours to judge | |
| 4 | Delete your company's name from a rejection — does it still make sense? | If yes, that reason is timeless and therefore empty | |
| 5 | Version C contains zero hedges | `grep -nEi "of course|however|that said|admittedly|arguably|to be fair" version-c.md` | |
| 6 | A reviewer could argue one of your flip conditions is *already true* | If not, your conditions are too far away to be useful | |

## Item 4 is the one the script cannot do

The script checks that a rejection contains *a* fact. It cannot check that the
fact is load-bearing. A rejection can quote your `max_connections` and still be a
category judgement if the number is decoration around "Redis adds an operational
dependency."

The reliable test is subtraction, and it takes ten seconds per rejection: cover
the numbers with your hand and read what is left. If what is left is a true
sentence about the technology, the numbers were decoration. If what is left is
incoherent without them, the rejection is about your system.

## Item 6 is the one that decides whether any of this was worth writing

A flip condition nobody will ever observe is a decoration with a date on it. For
each one, answer two questions in writing:

- **Who would notice?** Name a person or a graph. "The team" does not notice
  things; a panel with a threshold does, and a person with a calendar reminder
  does.
- **How far away is it?** A condition that is one quarter away is a tripwire. A
  condition that requires the company to double is a way of saying no politely.

If every flip condition is far away, the section reads as thorough and functions
as closed. That is the failure mode this rubric item exists to catch — and it is
invisible to every other check on this page.

## Mechanical checks, all of them

```sh
# from the 09-writing directory
sh 02-rejected-alternatives/tools/flip-check.sh \
   02-rejected-alternatives/worked-example/version-a.md \
   02-rejected-alternatives/worked-example/version-b.md   # the contrast, first
sh 02-rejected-alternatives/tools/flip-check.sh           # then your own drafts

cd artifacts/02-alternatives
diff -u version-a.md version-b.md | less                  # the diff is the lesson
grep -nEi "of course|however|that said|admittedly|arguably|to be fair" version-c.md
```

Then circulate version B with the Topic 1 doc, and log the row in
[`../log.md`](../log.md). The prediction table in [`README.md`](README.md) gets
filled **before** you send.
