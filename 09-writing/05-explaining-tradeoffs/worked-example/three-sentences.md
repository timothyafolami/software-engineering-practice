> **Worked example, not your artifact.** Yours goes in
> `artifacts/05-tradeoff/three-sentences.md`. Numbers are placeholders — an
> invented number here is worse than no number, because this is the version
> someone repeats in a meeting you are not in.
>
> Audience: a real named person who is not an engineer, and for whom "the
> ninety-ninth percentile" is genuinely not a unit.

**I want to spend `<n>` weeks fixing checkout slowness, starting next sprint.**
Right now about `<your measured share>` of checkouts take more than
`<threshold>` seconds, and it gets worse as we add traffic — this is the thing
behind the "site feels slow" tickets from the last month. **The cost is `<n>`
weeks of my time and one deploy with a rollback plan; the risk of waiting is
that it degrades further on its own rather than staying where it is.** I need a
yes or no by `<date>` to fit it into the sprint. (Happy to go into the technical
detail if useful — short version, one of our internal calls blocks everything
else while it waits.)

---

## What changed from the engineer-native version

The facts are identical. What moved:

| | Engineer-native | This version |
|---|---|---|
| Sentence 1 | the mechanism | the decision |
| Units | milliseconds and percentiles | weeks, share of checkouts, seconds a customer waits |
| The ask | absent | a yes or no, with a date |
| Mechanism | mandatory, first | available on request, last, one sentence |
| Cost of waiting | implied | stated, and stated honestly |

Nothing was dumbed down. One sentence was made available on request instead of
mandatory.

## The sentence that is doing the most work

> **the risk of waiting is that it degrades further on its own rather than
> staying where it is.**

It is the only sentence here that could be wrong, and it is the one your reader
is actually deciding on. If the honest version is "it may stay about the same,"
write that instead — you lose this argument and win the next three, because the
person learns that your risk statements track reality rather than what you want
to build.
