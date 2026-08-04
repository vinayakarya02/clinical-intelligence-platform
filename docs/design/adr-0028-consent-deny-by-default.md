# ADR-0028: Consent is deny-by-default, evaluated at disclosure, and break-glass audits before it returns

**Status:** Accepted (Phase 6)

## Context

Holding a record and being permitted to disclose it are different questions. The second depends
on who is asking, for what purpose, about which data, and what the patient has said — and only
the first of those is known at authentication time.

The common implementation checks consent when data is *ingested* or when a session *starts*.
Both are too early: the same authenticated clinician may legitimately see a patient at 09:00
for treatment and illegitimately look them up at 17:00 out of curiosity, and a session-time
check cannot tell those apart.

## Decision

**Deny by default.** A disclosure proceeds only if an applicable consent permits it. The three
outcomes are distinguished and never collapsed:

| Outcome | Meaning | Operational response |
|---|---|---|
| `permitted` | A consent applies and allows this | Disclose |
| `denied` | A consent applies and forbids this | Do not disclose; the patient decided |
| `no_consent_on_file` | Nothing applies | Do not disclose; **someone must obtain consent** |

Collapsing the last two into "denied" hides a fixable operational gap behind a patient's choice.

**Evaluated at the point of disclosure**, with purpose of use as a required parameter of the
request. A caller that does not state a purpose gets a refusal, not a default of "treatment".

**Break-glass is a purpose of use.** It grants access despite a denying consent, and it:

- requires a named human principal — never a service account, because break-glass exists to be
  answered for
- requires a free-text reason, stored verbatim
- writes a high-severity audit record **before the data is returned**, so a failure to audit is
  a failure to disclose rather than an undetected disclosure
- is reported to a review queue, because unreviewed break-glass is indistinguishable from no
  access control at all

**Revocation is immediate and forward-only.** A revoked consent stops the next disclosure. It
does not and cannot unmake earlier ones, and the audit trail retains both the disclosure and the
later revocation rather than rewriting history to look compliant.

## Consequences

- Every read path costs a consent evaluation. It is in-process and sub-millisecond; the
  alternative is a permission system that is not actually enforced.
- Emergency access works, is loud, and is reviewable. That is the correct trade — a
  break-glass mechanism nobody can use gets worked around, and one nobody reviews is a
  formality.
- Purpose of use must be threaded from the caller through to the disclosure. This is deliberate
  friction: a purpose the system infers is a purpose nobody stated.
- Regional variation (state law, GDPR, 42 CFR Part 2) is expressed as additional policy layers
  that can only further restrict, never widen, what consent permits.
