"""
Layer 10 - Topic 5: is your eval set already in your training data?

What this demonstrates
    MinHash, written out rather than imported, because the derivation is
    the part you need when somebody asks "how wrong could this be".

    For k independent hash permutations, the probability that two sets
    share a minimum is exactly their Jaccard similarity, so the fraction of
    matching signature positions is an unbiased estimate of J. Its standard
    error is about sqrt(J(1-J)/k) -- it falls as 1/sqrt(k), which is the
    number to quote. k=128 buys roughly 4% at J=0.5; k=512 buys roughly 2%,
    for four times the memory. That is the whole trade.

    The eval set here is built with a known amount of contamination --
    exact duplicates, near-duplicates with a few words changed, and clean
    items -- so the estimate can be checked against a truth this file
    computes exactly.

What to look for
    - `estimated J` against `exact J`, and the error against the 1/sqrt(k)
      prediction. If the errors are much larger than predicted, your hash
      functions are not independent enough (a common bug when they are
      derived by seeding one hash with i).
    - The recovered contamination count at threshold 0.8 against the known
      one. Near-duplicates are the ones that matter: an exact duplicate is
      easy to find with a hash set and nobody ships those any more.
    - The banding section. All-pairs is O(n^2) and does not survive
      contact with a real corpus; LSH banding turns it into a bucket
      lookup, at the cost of a stated false-negative rate.

    Run this BEFORE trusting any number in topic 6. An eval score on
    contaminated data is not an eval score; it is a memorisation test with
    a misleading name.

Standard library only. Runs with no arguments:
    python3 python/minhash_contamination.py
"""

from __future__ import annotations

import hashlib
import random
import statistics

SEED = 20260818
SHINGLE = 3           # words per shingle
K = 128               # hash permutations in the signature
BANDS = 32            # LSH bands; rows per band = K / BANDS
THRESHOLD = 0.8

WORDS = ("model latency cache token batch queue drift eval prompt kernel "
         "shard replica index vector gradient corpus sample metric budget "
         "schema pipeline weight tensor").split()


def shingles(text: str) -> set[str]:
    words = text.split()
    return {" ".join(words[i:i + SHINGLE])
            for i in range(max(1, len(words) - SHINGLE + 1))}


def hash_with(seed: int, value: str) -> int:
    """One of k independent hash functions.

    Salting a cryptographic hash with the permutation index gives
    functions that are independent enough for MinHash. Deriving them as
    (a*h + b) mod p from a single base hash is the usual shortcut and the
    usual source of correlated estimates.
    """
    return int.from_bytes(
        hashlib.blake2b(value.encode(), digest_size=8,
                        salt=seed.to_bytes(8, "little")).digest(), "little")


def signature(text: str, k: int = K) -> list[int]:
    sh = shingles(text)
    return [min(hash_with(i, s) for s in sh) for i in range(k)]


def estimate_jaccard(a: list[int], b: list[int]) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def exact_jaccard(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb)


def make_document(rng: random.Random, length: int = 60) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(length))


def perturb(rng: random.Random, text: str, edits: int) -> str:
    words = text.split()
    for _ in range(edits):
        words[rng.randrange(len(words))] = rng.choice(WORDS)
    return " ".join(words)


def main() -> None:
    rng = random.Random(SEED)

    training = [make_document(rng) for _ in range(400)]

    # A known contamination profile, so the estimate has something to be
    # checked against.
    evaluation: list[tuple[str, str]] = []
    for i in range(20):
        evaluation.append((training[i], "exact duplicate"))
    for i in range(20, 60):
        evaluation.append((perturb(rng, training[i], edits=3), "near duplicate"))
    for _ in range(140):
        evaluation.append((make_document(rng), "clean"))
    rng.shuffle(evaluation)
    known_contaminated = sum(1 for _, kind in evaluation if kind != "clean")

    print("Eval-set contamination via MinHash")
    print(f"  training corpus : {len(training)} documents")
    print(f"  eval set        : {len(evaluation)} items "
          f"({known_contaminated} contaminated by construction)")
    print(f"  shingles        : {SHINGLE}-word, k = {K} permutations, "
          f"threshold J >= {THRESHOLD}")

    # Pairs chosen to span the range of J, so the error check is not
    # dominated by the easy cases. A base document against itself (J = 1),
    # against light and heavy perturbations, and against an unrelated one.
    base = training[0]
    probe_pairs = [
        (base, base),
        (base, perturb(random.Random(1), base, edits=3)),
        (base, perturb(random.Random(2), base, edits=10)),
        (base, perturb(random.Random(3), base, edits=25)),
        (base, make_document(random.Random(4))),
    ]
    print("\n  Estimator accuracy -- predicted standard error is "
          "sqrt(J(1-J)/k), per pair:")
    print(f"    {'exact J':>9}" + "".join(f"{'k=' + str(k):>20}"
                                          for k in (32, 128, 512)))
    for a, b in probe_pairs:
        j = exact_jaccard(a, b)
        cells = []
        for k in (32, 128, 512):
            est = estimate_jaccard(signature(a, k), signature(b, k))
            predicted = (j * (1 - j) / k) ** 0.5
            cells.append(f"{est:>9.3f} (±{predicted:.3f})")
        print(f"    {j:>9.3f}" + "".join(f"{c:>20}" for c in cells))
    print("    The estimate should sit within a couple of standard errors of")
    print("    the exact value, and the error should halve as k quadruples.")

    # All-pairs at k = K, which is fine at this size and is not fine at
    # corpus scale -- see the banding section below.
    train_sigs = [signature(t) for t in training]
    print(f"\n  {'eval item kind':<18} {'count':>6} {'flagged':>8} "
          f"{'mean best J (est)':>19}")
    print("  " + "-" * 56)
    flagged_ids = set()
    by_kind: dict[str, list[float]] = {}
    for idx, (text, kind) in enumerate(evaluation):
        sig = signature(text)
        best = max(estimate_jaccard(sig, ts) for ts in train_sigs)
        by_kind.setdefault(kind, []).append(best)
        if best >= THRESHOLD:
            flagged_ids.add(idx)
    for kind in ("exact duplicate", "near duplicate", "clean"):
        vals = by_kind.get(kind, [])
        flagged = sum(1 for idx, (_, k2) in enumerate(evaluation)
                      if k2 == kind and idx in flagged_ids)
        print(f"  {kind:<18} {len(vals):>6} {flagged:>8} "
              f"{statistics.fmean(vals) if vals else 0:>19.3f}")

    true_positives = sum(1 for idx, (_, kind) in enumerate(evaluation)
                         if kind != "clean" and idx in flagged_ids)
    false_positives = sum(1 for idx, (_, kind) in enumerate(evaluation)
                          if kind == "clean" and idx in flagged_ids)
    print(f"\n  flagged {len(flagged_ids)} of {len(evaluation)} eval items "
          f"({100 * len(flagged_ids) / len(evaluation):.1f}%)")
    print(f"  recall on known contamination: {true_positives}/{known_contaminated}"
          f"   false positives: {false_positives}")
    print("  Read the recall against the `mean best J` column above before")
    print("  concluding the estimator failed. Three-word edits put the near")
    print("  duplicates just under the threshold, so this is the THRESHOLD")
    print("  choosing what counts as contamination -- which is a decision you")
    print("  own and should state, not a property of MinHash.")

    print("\n  LSH banding -- what makes this survive a real corpus")
    rows = K // BANDS
    print(f"    k = {K}, bands = {BANDS}, rows per band = {rows}")
    print(f"    A pair is a candidate if ANY band matches entirely, so the")
    print(f"    probability of becoming a candidate is 1 - (1 - J^r)^b:")
    print(f"      {'J':>6} {'P(candidate)':>14}")
    for j in (0.3, 0.5, 0.7, 0.8, 0.9):
        p = 1 - (1 - j ** rows) ** BANDS
        print(f"      {j:>6.1f} {p:>14.3f}")
    print("    The S-curve is the knob: more rows per band sharpens it, more")
    print("    bands lowers it. Pick the pair that puts the steep part at your")
    print("    threshold, and quote the false-negative rate that comes with it.")
    print("    All-pairs is O(n^2); this is a bucket lookup with a stated miss")
    print("    rate, and the stated miss rate is the honest part.")


if __name__ == "__main__":
    main()
