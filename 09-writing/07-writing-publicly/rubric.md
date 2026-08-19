# Topic 7 rubric — score the draft against the floor, not against your taste

| # | Check | How to test it | Score |
|---|---|---|---|
| 1 | At least one number you personally measured, with the conditions stated — machine, version, load shape | `sh tools/scarcity-check.sh <file>` | |
| 2 | A reader could reproduce your result, or at least its shape | Is the script, query or compose file attached or quoted? | |
| 3 | Something that went wrong, or that you got wrong, is in it | Find the section. If there is none, you published only the tidy half | |
| 4 | A claim specific enough that someone could show up and prove you wrong | Negate it. Would anyone write the negation? | |
| 5 | Sanitisation gate run **before** writing, with sign-off where required | `python3 lab/tools/sanitise_gate.py` plus its manual list | |
| 6 | Every number you did *not* measure carries its source | Same script; also the rule from [`../lab/README.md`](../lab/README.md) | |

## The one test that decides whether to publish at all

> *What is in here that could not have been generated without access to my
> machine, my production system, or my mistakes?*

Write the answer as one line at the top of the draft. If it is hard to write, the
fix is to go and run something, not to write harder — and that is a result, not a
failure. It is the same finding as an empty "scarce thing" column in
[`../log.md`](../log.md).

## The deletion exercise, which is faster than editing

Delete every sentence a model could have written without access to your machine.
Read what is left. If it is under a paragraph, the post *is* that paragraph plus
supporting material, and the restructure is: lead with it, and demote everything
you deleted to context — or drop the context entirely, because the reader can
generate it themselves at zero cost.

## Borrowed numbers are not yours

This lab records a defect worth publishing: a benchmark that reported zero lost
updates because the optimiser had removed the increment, and a very different
figure at `-O0`. The figure and its provenance are in the root
[`README.md`](../../README.md) and [`PREDICTIONS.md`](../../PREDICTIONS.md).

Publishing that number without rerunning it makes you the second person to
publish a benchmark you did not run, in a post about the danger of publishing
benchmarks you did not run. Rerun it, on your machine, and publish yours with the
compiler version and the build flags.

## Distribution is a separate problem, and it disambiguates silence

Send the post directly to three engineers you know: *"is there anything here you
would push back on?"*

- **They find plenty, the internet finds nothing** → distribution problem, the
  writing is fine.
- **They find nothing** → the post confirms what everyone already believed. A
  content problem, and no amount of distribution fixes it.

Silence without that test tells you nothing at all, and the two diagnoses call
for opposite responses.

## Mechanical checks

```sh
# from the 09-writing root, BEFORE you write
python3 lab/tools/sanitise_gate.py

# then, on the draft
sh 07-writing-publicly/tools/scarcity-check.sh artifacts/07-posts/<yyyy-mm>-<slug>.md
python3 lab/tools/sanitise_gate.py artifacts/07-posts/<yyyy-mm>-<slug>.md
```

Then publish, send to three people, and fill the second table in
[`../log.md`](../log.md): month, post, the scarce thing in it, where it came from.
