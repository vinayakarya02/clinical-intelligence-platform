# Clinical Ontology Licensing

**Status:** Phase 0 — Design only, not legal advice
**Added:** Phase 0 review found the document set treated SNOMED CT, UMLS, RxNorm, LOINC as freely
embeddable, when they carry real licensing obligations — see
[phase-0-architecture-review.md](../design/phase-0-architecture-review.md) finding A2. This
document names the dependency; actual license execution is a legal/procurement task, not an
engineering one.

## 1. Licensing status by ontology

| Ontology | License | Cost | Redistribution constraint |
|---|---|---|---|
| **SNOMED CT** | SNOMED International Affiliate License | Free for use *within* a UMLS-member country (includes the US, via the National Library of Medicine's UMLS license, which bundles SNOMED CT US Edition); **separate national licensing required** for non-member-country use, with country-specific fees | Redistribution of the raw terminology to a third party generally requires the recipient to hold their own Affiliate License — the platform can use SNOMED CT internally to code data, but cannot freely re-export raw SNOMED content to a tenant who isn't independently licensed |
| **UMLS Metathesaurus** | UMLS Metathesaurus License (NLM) | Free, but requires an annual license agreement and usage reporting | Bundles multiple source vocabularies (including SNOMED CT US Edition, RxNorm) under one agreement; some bundled source vocabularies carry their own additional restrictions the umbrella license does not override |
| **RxNorm** | Public domain (NLM) | Free | None beyond standard NLM terms |
| **LOINC** | Regenstrief Institute LOINC License | Free, requires acceptance of license terms and attribution | Redistribution of the full LOINC table requires the same attribution/license-acceptance flow to be presented to any redistribution recipient |
| **ICD-10/11** | WHO (ICD-11) / national adaptation bodies (ICD-10-CM in the US, ICD-10-CA in Canada, etc.) | Generally free for the WHO base classification; national clinical modifications may have their own stewardship terms | Regional ontologies (OPCS-4 UK, ICD-10-CA) each carry their own licensing — see §3 |
| **HPO** | Creative Commons (CC BY 4.0) | Free | Attribution required |

## 2. Platform-level implications

- **US-based tenants**: covered under the platform's own UMLS Metathesaurus License (which
  bundles SNOMED CT US Edition and RxNorm) — no per-tenant licensing action needed.
- **Non-US, UMLS-member-country tenants**: still require the platform to hold a valid Affiliate
  License recognized in that country; most major healthcare markets (UK, Canada, Australia,
  much of the EU) are UMLS members, but licensing terms and any national fee schedule vary and
  must be checked per country before onboarding a tenant there — this is a named step in
  [docs/operations/tenant-lifecycle.md §1](../operations/tenant-lifecycle.md#1-onboarding),
  not an assumption.
- **Non-UMLS-member-country tenants**: SNOMED CT is **not** freely usable; a separate national
  licensing agreement (where one exists) or an alternative ontology strategy is required before
  onboarding — this is a legal/business-development blocker discovered at sales time, not an
  engineering task, and must be flagged during the sales cycle rather than assumed away.

## 3. Regional/local ontology extension

Per the ontology-registry pattern in
[03-knowledge-graph.md §1](../architecture/03-knowledge-graph.md#1-graph-construction-pipeline-graphrag-pattern),
adding a new regional ontology (OPCS-4 for UK procedures, ICD-10-CA for Canada, JMDC codes for
Japan) is a data/config change, not a code change — but each such addition still carries its own
licensing review, tracked the same way as the ontologies in §1 before the registry entry is
activated for any tenant.

## 4. Related documents

- [Knowledge Graph Design](../architecture/03-knowledge-graph.md)
- [Tenant Lifecycle](../operations/tenant-lifecycle.md)
- [SLA, DR, incident response & cost model — cost model §6](../operations/sla-dr.md#6-cost-model)
