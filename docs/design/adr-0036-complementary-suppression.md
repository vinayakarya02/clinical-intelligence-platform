# ADR-0036: Cell suppression must be complementary, or it is arithmetic

**Status:** Accepted (Phase 7)

## Context

Small-cell suppression is the standard control for aggregate health statistics: a cell with
fewer than *k* subjects is withheld, because a count of two in a rare-diagnosis-by-postcode cell
identifies two people.

Phase 6 implemented primary suppression for population analytics. Applied naively — and this is
how it is usually applied — it does not work.

Consider a table published with a total:

| Site | Patients |
|---|---|
| North | 42 |
| South | 38 |
| East | **suppressed** (< 11) |
| **Total** | **87** |

The East cell is recoverable by subtraction: 87 − 42 − 38 = 7. The suppression is decorative.
The same defeat works across a published time series, across two dashboards sharing a
denominator, and across a report run before and after a single patient was added.

This is not a subtle theoretical attack. It is the first thing anyone does with a suppressed
table.

## Decision

Suppression is applied as a **set operation over the whole result**, never per cell:

1. **Primary suppression** — cells below the threshold are withheld.
2. **Complementary suppression** — if a suppressed cell is recoverable from the other published
   cells and any published total, additional cells are suppressed until it is not. The
   complementary cell chosen is the smallest one, because suppressing the smallest loses the
   least information.
3. **Totals are suppressed too** when they would otherwise permit the subtraction, rather than
   published on the assumption nobody will do it.
4. A result where suppression cannot be made safe is **refused entirely**, with the reason, in
   preference to publishing something that leaks.

The number of suppressed cells is reported. A consumer who does not know a table was suppressed
will read the visible cells as the complete picture and compute their own — wrong — total.

**Differential privacy is not claimed.** This is deterministic cell suppression, which is a
weaker guarantee: it defends against the arithmetic above and against small-cell identification,
and it does not defend against an attacker with strong auxiliary information or against
differencing across many correlated queries. Claiming otherwise would be asserting a formal
property that is not implemented.

## Consequences

- Suppression cannot be a display-layer concern, because the display layer does not see the
  whole result set. It runs in the query executor, above the store and below every surface.
- Tables become less informative than they could be, and sometimes noticeably so. That is the
  cost of the guarantee.
- Because suppression depends on the whole result, the same metric grouped two ways can suppress
  different cells. That is correct and will look inconsistent to users, so the suppression count
  and threshold are reported alongside every result.
- The threshold is configuration, not a constant: eleven is a common default in US health
  statistics, and other jurisdictions and datasets use other values.
