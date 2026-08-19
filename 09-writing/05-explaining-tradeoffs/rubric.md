# Topic 5 rubric — score both versions before you send either

| # | Check | How to test it | Score |
|---|---|---|---|
| 1 | Decision in sentence one — not context, not background | Read only sentence one aloud. Does it contain what you want to do? | |
| 2 | Cost in money, time or risk, never in a technical unit | Find the cost sentence. What unit is it in? | |
| 3 | A specific thing you need, with a date | Search for the date | |
| 4 | Mechanism at most once, at the end, one sentence, with an offer to expand | Count mechanism sentences | |
| 5 | Zero jargon that survives find-and-replace with a plain phrase | `sh tools/jargon-check.sh <file>` | |
| 6 | The cost of *not* doing it is stated, honestly, including "it may stay about the same" if that is the truth | Find that sentence. Is it the one you wish were true, or the one that is? | |
| 7 | Every number is one you measured and you can say where it came from | You will be asked, in the room where you cannot check | |

## The check that is not on the list, because it happens afterwards

Send the three-sentence version and ask the two questions that produce data
rather than agreement:

1. "What would you tell your own manager this is about?"
2. "What is the strongest argument against doing this now?"

Record both **verbatim** in [`restatement-form.md`](restatement-form.md), copied
to `artifacts/05-tradeoff/restatement.md`. Your summary of their answer will
unconsciously repair their misunderstanding, and that misunderstanding is the
measurement.

## Item 2 has a trap in it

The honest cost of `<n>` weeks of your time is not `<n>` weeks. It is the named
thing that does not happen instead. A cost stated as "two weeks" invites the
reply "fine, do it in the background"; a cost stated as "two weeks, which is
`<the feature>` slipping to `<date>`" is the trade your reader is actually
making, and it is the version they can say no to intelligently.

Stating it that way costs you approvals you would have got by being vague. It
buys you a reader who believes your next estimate.

## Item 7 and the number you do not have

Three responses to "what is the revenue impact?", in order of how well they age:

1. **Invent it.** Works once. The cost arrives three months later, applied to
   every number you have ever given them.
2. **Refuse.** Honest, and it ends the conversation with nothing gained.
3. **"I can't tell you that. Here is what I can tell you: `<the measured
   thing>`. To answer your question I would need `<the number>`, which
   `<person>` has — want me to go and get it?"** Honest, and it frequently
   produces the number, because the person who owns it is usually in the room.

## Mechanical checks

```sh
# from 09-writing/05-explaining-tradeoffs
sh tools/jargon-check.sh worked-example/three-sentences.md   # calibrate: zero hits
sh tools/jargon-check.sh                                     # then your own drafts
```

Then send, ask the two questions, fill the record verbatim, and log the row in
[`../log.md`](../log.md).
