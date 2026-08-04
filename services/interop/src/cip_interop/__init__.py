"""Clinical ecosystem interoperability.

Phase 6. Everything between "another organisation's system has a fact" and "this platform holds
it, correctly attributed, lawfully disclosed, and traceable back to the wire".

Two findings shape the design and are worth stating before any API is read:

Patient identity does not survive an organisational boundary — published duplicate rates inside
one organisation run near 18%, and cross-organisation matching as low as 50%. So the EMPI has a
human review zone and merges are reversible (docs/design/adr-0027-empi-review-not-automerge.md).

Holding a record and being permitted to disclose it are different questions. So consent is
deny-by-default and evaluated at the point of disclosure, never at ingestion or session start
(docs/design/adr-0028-consent-deny-by-default.md).

**Nothing here has exchanged a message with a real hospital system.** Every conformance claim is
against a specification document, not against a counterparty. See
docs/design/phase-6-engineering-report.md §11.
"""

from cip_interop.domain import (
    Address,
    AdministrativeSex,
    ContactPoint,
    HumanName,
    Identifier,
    InteropError,
    PersonRecord,
    PurposeOfUse,
    SourceSystem,
)

__all__ = [
    "Address",
    "AdministrativeSex",
    "ContactPoint",
    "HumanName",
    "Identifier",
    "InteropError",
    "PersonRecord",
    "PurposeOfUse",
    "SourceSystem",
]
