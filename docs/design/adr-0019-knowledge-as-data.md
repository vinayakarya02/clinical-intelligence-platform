# ADR-0019: Clinical knowledge is data, never code

**Status:** Accepted (Phase 5)

## Context

The decision engine needs rules ("potassium above X on an ACE inhibitor → repeat the test"),
guidelines, drug interactions, dose limits, and risk models. The obvious implementation is
Python: a function per rule, readable by the engineers who maintain it.

That is wrong for three separate reasons, and any one of them is sufficient.

**A clinical reviewer cannot read Python.** Clinical content must be reviewed by a clinician,
and content they cannot read is content that has not been reviewed — whatever the sign-off
says.

**Organisations disagree.** NICE and WHO differ. A US health system and an NHS trust have
different formularies, different thresholds, and different escalation paths. Clinical content
compiled into the application can serve exactly one of them.

**Provenance.** Every recommendation must trace to a citation and a version. A rule in code
traces to a commit, which is not a clinical citation and means nothing in a review.

## Decision

Every clinical fact is a versioned, cited, dated artifact loaded from configuration. The
engine contains no clinical content whatsoever — it contains the machinery for evaluating
content it is given.

Each artifact carries `id`, `version`, `effective_from`, optional `effective_until`,
`citations`, and `severity`/`evidence_quality` where applicable. Loading validates the schema
and **refuses an artifact with no citation**, because an uncited clinical assertion cannot be
reviewed or defended.

Conditions are expressed in a small typed expression language evaluated by an interpreter over
an explicit AST — **never `eval`**. A knowledge base is a file an operator can edit, and `eval`
over operator-editable content is remote code execution with a clinical veneer.

## Consequences

- The knowledge base can be reviewed, replaced, versioned, and audited independently of a
  release.
- The expression language is deliberately small: comparison, membership, presence, temporal
  windows, and boolean composition. Anything it cannot express is a signal that the rule needs
  a new *operator* — reviewed once — rather than an escape hatch.
- **The corpus shipped in this repository is a demonstration set and has not been clinically
  reviewed.** That is stated in the safety case, in the loader's docstring, and in every
  knowledge file's header, because the failure mode of a convincing demo corpus is that
  somebody deploys it.
