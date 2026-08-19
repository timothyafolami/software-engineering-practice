> **Worked example, not your artifact.** Yours goes in
> `artifacts/05-tradeoff/one-pager.md`. Every number is a placeholder; fill from
> measurement or delete the sentence. This is the version you send *after* they
> reply to the three sentences, or bring to the meeting — not instead of it.

# Checkout slowness: what I want to do, what it costs, what happens if we wait

**The decision.** Spend `<n>` weeks of engineering time, starting `<sprint>`,
making checkout stop getting slower. I need a yes or no by `<date>`.

**What customers see today.** About `<your measured share>` of checkouts take
longer than `<threshold>` seconds. It is worst at our busiest hour and better
overnight, which is why it shows up as "the site feels slow sometimes" rather
than as an outage. `<n>` support tickets in the last month describe it.
(Source for both numbers: `<dashboard query>` and `<ticket search>`. Ask me and
I will show you the graph.)

**What it costs.** `<n>` weeks of my time — which is `<the named feature or work
that slips>`, and that is the real price, not the calendar. One deploy, released
gradually, with a switch that turns it off in under two minutes if it misbehaves.
No customer data changes, and nothing about how data is stored changes either.

**What happens if we wait.** It gets worse rather than staying flat: the slowness
comes from work waiting in line behind one slow step, and the line grows faster
than the traffic that feeds it. I cannot tell you the month it becomes an outage — I do
not have that number and I am not going to invent it. What I can tell you is the
direction, and that the same thing measured `<n>` months apart moved `<the
observed direction>`.

**What I cannot tell you.** The revenue impact. To answer that I would need
`<the conversion number>`, which `<team or person who owns it>` has — if that
number matters to the decision, it is one question away and I will go and ask it.

**What I am not asking for.** Not a rewrite, not new infrastructure, not
headcount. One team's time, for `<n>` weeks, on a change we can turn off.

**If you want the technical version.** One of our internal calls waits for
another system, and while it waits it holds up everything else on that server —
including customers who are not buying anything. The fix is to make that call
wait without holding everything else up, and to give it a deadline after which we
stop waiting. Happy to go deeper.

---

## Rubric check, run on this page

1. **Decision in sentence one.** Yes — first line under the title.
2. **Cost in money, time or risk.** Time, plus the named work that slips. The
   second half is the one people leave out, and it is the half your reader is
   actually trading against.
3. **A specific thing you need, with a date.** Yes or no, by `<date>`.
4. **Mechanism once, at the end, with an offer.** Last paragraph.
5. **Zero jargon.** `sh tools/jargon-check.sh worked-example/one-pager.md`
6. **Cost of not doing it, stated honestly.** Direction stated, timing declined.
7. **Every number measured, with a source you can produce.** Sources named
   inline; placeholders until you fill them.

Two things worth stealing. **"What I cannot tell you"** is a section, not an
apology — it converts a gap into a specific request for someone else's number,
and it is the sentence that makes you believable about the numbers you do have.
**"What I am not asking for"** pre-empts the reader's largest fear, which is
usually that this is the thin end of a rewrite.
