# Topic 6 rubric — score a message before you write it, not after

| # | Check | How to test it | Score |
|---|---|---|---|
| 1 | The subject says what the **change** does, not what file it touches | Read the subject alone. Could a stranger tell what behaviour changed? | |
| 2 | The body answers "why now" | Search for the trigger: an incident, a report, a measurement, a date | |
| 3 | A rejected approach is named — aim for 3 in every 10 commits | `sh tools/archaeology.sh` counts this over your last 50 | |
| 4 | A stranger with `git blame` and no access to you gets their question answered | The timed retrieval test. This is the only honest version | |
| 5 | If an agent wrote the body, the diff summary has been **replaced** by the reasoning only you have | Delete every sentence recoverable from the diff. Is anything left? | |
| 6 | Every number says where it came from | Same rule as the rest of the layer | |

## Item 4 is the only measurement, and you cannot run it on yourself today

You wrote the message, so you supply the missing context for free and every
message looks adequate. The test needs distance: code at least six months old,
ideally not yours, and a stopwatch.

```sh
cd <your production service repo>
sh <path>/09-writing/06-commits-and-prs/tools/archaeology.sh                 # baseline
sh <path>/09-writing/06-commits-and-prs/tools/archaeology.sh app/pricing.py 120 124
```

Three lines gives you a fraction. That fraction, and the median seconds, are what
the prediction table in [`README.md`](README.md) asks for — filled in before you
run the attempts.

## Item 5, stated precisely

An agent's PR body can be accurate, complete, well-written, and worth nothing,
because it summarises the diff — and the diff is already a perfect record of what
changed. What it cannot know:

- **why now** — the trigger existed outside the repository;
- **what you rejected** — the branch you deleted before committing;
- **the constraint** — an invariant that is true of the *world*, not the code:
  "the caller retries, so this must stay idempotent";
- **what you actually ran** — as opposed to what the tests cover.

Those four are the whole human contribution to the record now. If your body has
none of them, an agent could have written it, and something did.

## The four things, ranked for when you are in a hurry

Only one of the four is even partially recoverable from the code — the
constraint, and only by a reader who already suspects it is there. So when you
have thirty seconds:

1. **The constraint**, because losing it costs a bug rather than an hour.
2. **What you rejected**, because it is the only thing stopping the next person
   walking your dead end.
3. **Why now**, because it is what dates the decision and lets someone notice the
   trigger no longer applies.
4. **How you know it works**, because a reviewer can partly reconstruct it.

## Where your ecosystem puts the reasoning, and how it fails

One page each, with the archaeology commands and hunting grounds that suit it:

| | Convention | The reasoning usually lives | Fails when |
|---|---|---|---|
| [Python](python/commit-conventions.md) | `gh-NNNNN: summary`, ecosystem mandates nothing | the linked issue, or nowhere | the body is empty by convention and the tracker is gone |
| [Node.js](nodejs/commit-conventions.md) | Conventional Commits, machine-load-bearing | the PR thread on the forge | the prefix looks like compliance and the thread is not in the clone |
| [Go](golang/commit-conventions.md) | `package: summary`, one logical change | the commit body | a squashed branch pollutes `git log -L` for every line in it |
| [Rust](rust/commit-conventions.md) | linked RFC / tracking issue | the RFC | the link outlives the document it points at |
| [C++](cpp/commit-conventions.md) | `[component]` / `component:` with trailers | the commit body, because nowhere else can hold it | an invariant the compiler never checked goes unwritten |
| [Java](java/commit-conventions.md) | tracker id and synopsis, JEPs | the ticket, in a system you may not control | the tracker is consolidated and the key is remapped |

Each folder also holds a `commit-template.txt` shaped for that ecosystem:

```sh
git config commit.template <path>/09-writing/06-commits-and-prs/<lang>/commit-template.txt
```

That one line is the highest-leverage habit change in this topic, because it
moves the prompt to the moment you are already writing.
