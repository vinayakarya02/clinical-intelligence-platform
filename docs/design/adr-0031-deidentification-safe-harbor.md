# ADR-0031: Safe Harbor is implemented; Expert Determination is not claimed

**Status:** Accepted (Phase 6)

## Context

Research and analytics need data that is not PHI. HIPAA offers two routes: **Safe Harbor**,
which enumerates 18 identifier categories to remove, and **Expert Determination**, in which a
qualified person applies statistical methods and documents that re-identification risk is very
small.

Safe Harbor is mechanical and therefore implementable. Expert Determination is a professional
opinion about a specific dataset released into a specific context, and it depends on what else
the recipient already holds — which software cannot know.

The temptation is to implement a k-anonymity check, call the result "expert determined", and
release a richer dataset. That is a false compliance claim, and it is one whose falseness only
surfaces when someone is re-identified.

## Decision

Implement Safe Harbor, completely and testably: names, geography below state (ZIP truncated to
three digits, and suppressed entirely for the ~17 low-population prefixes), all date elements
reduced to year, ages over 89 aggregated to `90+`, and the remaining categories — telephone,
fax, email, SSN, MRN, health plan number, account number, licence, vehicle, device, URL, IP,
biometric, photograph, and any other unique identifier — removed.

The output carries a manifest naming the method (`safe_harbor`), the ruleset version, the
categories acted on, and a per-field record of what was removed or generalised.

**Expert Determination is not implemented.** The API has no value for it. A dataset that needs
it goes to a qualified expert, and the platform's job is to produce a reproducible extract for
that person to assess.

A **limited data set** is offered as a distinct, correctly-labelled third option — dates and
geography retained, direct identifiers removed — which is a real HIPAA category requiring a data
use agreement, and is labelled as requiring one.

## Consequences

- Safe Harbor output is lossy in ways that matter for research: exact dates are gone, so
  time-to-event analysis at day resolution is impossible on it. That is the trade Safe Harbor
  makes, and pretending otherwise would mean not implementing Safe Harbor.
- Re-identification risk is **not zero** even under Safe Harbor, and the manifest says so.
  Quasi-identifier combinations (rare diagnosis plus three-digit ZIP plus 90+) can single
  someone out. Claiming Safe Harbor means claiming the method was applied, not that
  re-identification is impossible.
- Because the transform is declarative and versioned, an extract can be reproduced exactly, and
  a change to the ruleset is a visible, dated event rather than a silent drift in what "de-
  identified" meant.
