> **Worked example, version A — first instinct, no self-editing.**
> This is an example, not your artifact; yours goes in
> `artifacts/02-alternatives/`. Numbers are placeholders on purpose — see
> [`../../lab/README.md`](../../lab/README.md), rule 1.
> Read A and B side by side: `diff -u version-a.md version-b.md`.

# Alternatives considered

**Alternative: increase the connection pool size.** Rejected — this just moves
the bottleneck and doesn't fix the underlying problem.

**Alternative: run the pricing call in a thread pool.** Rejected — thread pools
are a band-aid over a synchronous client and add complexity.

**Alternative: rewrite the checkout handler in Go.** Rejected — Go's scheduler
handles blocking calls better, but rewriting in another language is not
justified for one endpoint.
