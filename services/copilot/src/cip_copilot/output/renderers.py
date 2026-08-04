"""Answer rendering: Markdown, JSON, FHIR, and the API envelope.

Every renderer is a **pure projection** of one validated :class:`~cip_copilot.domain.Answer`.
None re-derives, re-ranks, or re-words anything, which is what guarantees the clinician
reading Markdown and the system consuming JSON are looking at the same answer. A renderer that
computed anything would be a second, untested implementation of the reasoning layer.

The FHIR projection is deliberately narrow: a ``DocumentReference`` for the answer text and a
``Provenance`` resource recording what it was derived from. It is not a clinical resource — an
assistant's answer is not an ``Observation`` or a ``Condition``, and emitting one would put
generated content into the record where a clinician recorded findings.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from typing import Any

from cip_copilot.domain import Answer, EvidenceKind, ResponseMode

__all__ = [
    "render_api_envelope",
    "render_fhir",
    "render_json",
    "render_markdown",
]


def render_markdown(answer: Answer, *, explanation_markdown: str = "") -> str:
    """Clinician-facing rendering: the answer, then how it was reached."""
    lines: list[str] = []

    if answer.mode is ResponseMode.BLOCKED:
        lines.append("**Cannot answer safely.**")
    elif answer.mode is ResponseMode.UNCERTAIN:
        lines.append("**Insufficient confidence to answer.**")
    elif answer.mode is ResponseMode.CLARIFICATION:
        lines.append("**Clarification needed.**")
    elif answer.mode is ResponseMode.NEEDS_APPROVAL:
        lines.append("**Awaiting approval.**")

    if answer.text:
        lines.append("")
        lines.append(_with_citations(answer))

    if answer.safety_findings:
        lines.append("")
        lines.append("**Safety notes**")
        for finding in answer.safety_findings:
            severity = getattr(finding, "severity", "")
            message = getattr(finding, "message", str(finding))
            lines.append(f"- _{severity}_: {message}")

    if explanation_markdown:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(explanation_markdown)

    return "\n".join(lines).strip()


def _with_citations(answer: Answer) -> str:
    """Append the citation list in presentation order.

    Numbering matches the order the evidence is listed, so a ``[2]`` in the prose and the
    second entry in the list are the same thing. Any other ordering reads as a hallucinated
    citation during review even when the retrieval was correct.
    """
    cited = answer.cited_evidence()
    if not cited:
        return answer.text

    lines = [answer.text, ""]
    lines.append("Sources:")
    for index, item in enumerate(cited, start=1):
        reference = f" — `{item.source_ref}`" if item.source_ref else ""
        lines.append(f"[{index}] {item.cite_label()}{reference}")
    return "\n".join(lines)


def render_json(answer: Answer) -> dict[str, Any]:
    """Full structured rendering, for an API or an audit sink."""
    return {
        "mode": str(answer.mode),
        "text": answer.text,
        "confidence": {
            "score": answer.confidence.score,
            "components": {k: round(v, 4) for k, v in answer.confidence.as_dict().items()},
            "weakest": answer.confidence.weakest(),
        },
        "uncertainty_reason": answer.uncertainty_reason,
        "claims": [
            {
                "id": claim.id,
                "statement": claim.statement,
                "evidence_ids": list(claim.evidence_ids),
                "support": str(claim.support),
                "verified": claim.verified,
                "notes": list(claim.verification_notes),
            }
            for claim in answer.claims
        ],
        "evidence": [
            {
                "id": item.id,
                "kind": str(item.kind),
                "content": item.content,
                "source_ref": item.source_ref,
                "document_id": str(item.document_id) if item.document_id else None,
                "section": item.section,
                "document_type": item.document_type,
                "effective_date": (
                    item.effective_date.isoformat() if item.effective_date else None
                ),
                "retrieval_score": item.retrieval_score,
                "confidence": item.confidence,
            }
            for item in answer.cited_evidence()
        ],
        "safety": [
            finding.to_json() if hasattr(finding, "to_json") else {"message": str(finding)}
            for finding in answer.safety_findings
        ],
        "trace": [record.to_json() for record in answer.trace],
        "usage": {
            "prompt_tokens": answer.usage.prompt_tokens,
            "completion_tokens": answer.usage.completion_tokens,
            "total_tokens": answer.usage.total_tokens,
            "model_calls": answer.usage.calls,
        },
        "prompt_versions": dict(answer.prompt_versions),
        "pending_approval": (
            {
                "step_id": answer.pending_approval.step_id,
                "tool": answer.pending_approval.tool_name,
                "reason": answer.pending_approval.reason,
            }
            if answer.pending_approval
            else None
        ),
    }


def render_api_envelope(answer: Answer, *, request_id: str | None = None) -> dict[str, Any]:
    """Compact envelope for a chat surface.

    Deliberately omits the trace and the full evidence bodies — a UI needs the answer, the
    citations, and enough confidence signal to render a caveat. The detail lives one fetch
    away rather than on every response, because sending an audit record to a browser on every
    turn is both slow and a wider PHI exposure than the surface needs.
    """
    cited = answer.cited_evidence()
    return {
        "request_id": request_id,
        "mode": str(answer.mode),
        "answer": answer.text,
        "confidence": answer.confidence.score,
        "uncertainty": answer.uncertainty_reason,
        "citations": [
            {"index": index, "label": item.cite_label(), "ref": item.source_ref}
            for index, item in enumerate(cited, start=1)
        ],
        "safety": [
            {
                "severity": str(getattr(finding, "severity", "info")),
                "message": getattr(finding, "message", str(finding)),
            }
            for finding in answer.safety_findings
        ],
        "has_explanation": bool(answer.claims),
    }


def render_fhir(
    answer: Answer,
    *,
    patient_id: uuid.UUID | None = None,
    author_device: str = "clinical-intelligence-platform",
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """A FHIR R4 ``Bundle`` with a ``DocumentReference`` and its ``Provenance``.

    ``DocumentReference`` rather than a clinical resource: an assistant's answer is a document
    about the record, not a finding in it. Writing it as an ``Observation`` would place
    generated content alongside values a clinician measured, and no downstream consumer could
    tell them apart.

    The ``Provenance`` resource is what makes the answer auditable inside the record — it
    names the derivation sources, so a reviewer reading the note can reach the evidence.
    """
    stamp = (now or dt.datetime.now(dt.UTC)).isoformat()
    document_id = str(uuid.uuid4())
    provenance_id = str(uuid.uuid4())

    subject = {"reference": f"Patient/{patient_id}"} if patient_id else None

    document: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "id": document_id,
        "status": "current",
        # `preliminary` is not a hedge: the content is machine-generated and has not been
        # reviewed by a clinician, and marking it `final` would assert an attestation that
        # nobody made.
        "docStatus": "preliminary",
        "type": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": "34109-9",
                    "display": "Note",
                }
            ],
            "text": "Clinical assistant response",
        },
        "date": stamp,
        "author": [{"display": author_device}],
        "content": [
            {
                "attachment": {
                    "contentType": "text/markdown",
                    "data": base64.b64encode(answer.text.encode("utf-8")).decode("ascii"),
                    "title": "Clinical assistant response",
                    "creation": stamp,
                }
            }
        ],
    }
    if subject:
        document["subject"] = subject

    derived_from = [
        {"reference": item.source_ref} if item.source_ref else {"display": item.id}
        for item in answer.cited_evidence()
    ]

    provenance: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": provenance_id,
        "target": [{"reference": f"DocumentReference/{document_id}"}],
        "recorded": stamp,
        "agent": [
            {
                "type": {
                    "coding": [
                        {
                            "system": (
                                "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
                            ),
                            "code": "assembler",
                        }
                    ]
                },
                "who": {"display": author_device},
            }
        ],
        "entity": [{"role": "source", "what": ref} for ref in derived_from],
    }

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": stamp,
        "entry": [{"resource": document}, {"resource": provenance}],
    }


def evidence_kind_counts(answer: Answer) -> dict[str, int]:
    """How many of each evidence kind the answer rests on. Used by the evaluation harness."""
    counts = dict.fromkeys((str(kind) for kind in EvidenceKind), 0)
    for item in answer.cited_evidence():
        counts[str(item.kind)] += 1
    return counts
