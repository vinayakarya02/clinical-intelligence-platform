# ADR-0027: The EMPI has a review zone, and merges are reversible links

**Status:** Accepted (Phase 6)

## Context

Patient matching is the hardest correctness problem in healthcare integration and the one with
the worst failure mode. Published figures put duplicate rates within a single organisation near
18%, and cross-organisation match rates as low as 50% even between sites running the same EHR
vendor.

Both errors are harmful and they are not symmetric in *kind*:

- A **false match** merges two people. The resulting chart contains someone else's allergies,
  diagnoses, and medications, presented with no indication that it is two people. This is the
  error that kills.
- A **false non-match** splits one person. Half the history is invisible at the point of care.
  Bad, but the clinician sees a thin record rather than a confidently wrong one.

A single threshold forces every ambiguous pair into one of these.

## Decision

**Fellegi–Sunter with two thresholds and three zones.** Field agreement contributes
`log₂(m/u)`; disagreement contributes `log₂((1−m)/(1−u))`. The sum is compared against an upper
and a lower threshold, giving *match*, *review*, and *non-match*.

The review zone is queued for a human. Queue depth is an operational metric with an alert,
because the failure mode of a review queue is that nobody looks at it.

**A merge is a link, not a rewrite.** Source records keep their identifiers and remain
individually addressable; a link record says which person they resolve to, who decided, when,
and why. Unmerge restores the prior state because the prior state was never destroyed.

**Deterministic identifiers promote, never demote.** A matching government or national health
identifier can raise a pair to *match*. A missing one can never lower a probabilistic match,
because most records simply do not carry one and treating absence as disagreement would break
matching for the majority to protect the minority.

**Missing fields are neutral.** A field absent on either side contributes zero, not a
disagreement weight. Absence is not evidence of difference — a record with no recorded middle
name does not disagree with one that has it.

## Consequences

- Throughput depends on a human queue. That is the honest cost of the error profile, and any
  design that removes it is choosing to auto-merge.
- The `m` and `u` probabilities are configuration, not constants — they are population
  properties, and a surname's discriminating power differs by region. Defaults ship, and are
  documented as defaults rather than as truth.
- Merge history is permanent and auditable. "Why is this record attached to this person" has an
  answer with a name and a timestamp on it.
- Blocking is required for scale (all-pairs is quadratic) and blocking loses recall by
  construction. The blocking keys are declared and their recall cost is stated rather than
  hidden.
