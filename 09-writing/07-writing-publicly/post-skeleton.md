# <Title: the claim, not the topic>

> Copy to `artifacts/07-posts/<yyyy-mm>-<slug>.md`.
>
> **Run the sanitisation gate before you write a word** — not after.
> `python3 lab/tools/sanitise_gate.py` from the `09-writing` root, and the
> checklist it prints at the end that no script can check. It is much harder to
> launder a draft than to write a clean one, and laundering fails in a specific
> way: you remove the identifying detail and keep the shape it was load-bearing
> for, ending up with a post that is both unsafe and unconvincing.

**The scarce thing in this post:** `<one line — what is here that could not have
been generated without access to my machine, my production system, or my
mistakes?>`

If that line is hard to write, stop. The fix is to go and run something, not to
write harder.

---

## The claim

One paragraph. Specific enough that someone can show up and prove you wrong.
Same test as [Topic 1](../01-the-design-doc/README.md): negate it, and ask
whether anyone would ever write the negation.

## The conditions

Machine, OS, architecture. Versions of everything that could plausibly matter —
runtime, driver, database, compiler, and the optimisation level if there is one.
Load shape and how it was generated. Anyone who cannot see this cannot check
you, and a number nobody can check is decoration.

## What I measured

The numbers, each with how it was produced. Not a table you assembled from
memory: the command, the query, the run.

## What I got wrong first

The wrong hypothesis, the broken harness, the measurement that turned out to be
measuring nothing. This is the scarcest section in the whole post, because the
incentive runs against writing it and therefore almost nobody does.

## How to reproduce it

The script, the compose file, the exact commands. If a reader cannot reproduce
the result, they can at least reproduce the *shape* of it — and the ones who try
are the ones who send back the most valuable replies.

## What I still don't know

The seam. Name it, and you will frequently get it answered by a stranger.

---

## Before publishing

- [ ] Sanitisation gate run **before** writing, and re-run on the final draft.
- [ ] Employer sign-off in writing, if any of this derives from a production
      incident.
- [ ] Every number is one I measured, or carries its source.
- [ ] Sent directly to three engineers I know, with the question: "is there
      anything here you would push back on?"
- [ ] Row added to `log.md`'s second table: month, post, the scarce thing, where
      it came from.
