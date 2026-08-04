"""FHIR validation.

Validates an instance against its declared definition: required elements, cardinality,
primitive syntax, required value-set bindings, reference target types, choice-group
exclusivity, and version applicability.

Two behaviours are deliberate and both are refusals.

**An unrecognised ``modifierExtension`` is an error, not a warning.** FHIR's rule is that a
consumer which does not understand a modifier extension must not process the resource, because
a modifier extension can invert the meaning of the element it sits on — "this allergy is
refuted" carried as a modifier on an active allergy. Treating it as an unknown-but-harmless
field is the one FHIR conformance failure that can produce a clinically inverted record.

**An element belonging to a different FHIR version is an error naming the version.** Accepting
an R4 ``medicationCodeableConcept`` on an R5 resource writes a field no R5 client reads, and
the resource looks fine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cip_interop.fhir.definitions import (
    ElementDefinition,
    ElementType,
    FhirVersion,
    definition_for,
)
from cip_interop.fhir.resources import Resource, parse_reference

__all__ = [
    "FhirIssue",
    "FhirValidationOutcome",
    "validate_resource",
]

#: Elements every resource may carry. Not in the definitions because they are not
#: resource-specific, and repeating them 18 times is 18 chances to omit one.
_COMMON_ELEMENTS = frozenset(
    {
        "resourceType",
        "id",
        "meta",
        "implicitRules",
        "language",
        "text",
        "contained",
        "extension",
        "modifierExtension",
    }
)

_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_DATETIME = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?)?)?$")
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_CODE = re.compile(r"^[^\s]+( [^\s]+)*$")
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")

#: Modifier extensions this platform understands. Anything else blocks the resource.
_KNOWN_MODIFIER_EXTENSIONS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FhirIssue:
    """One ``OperationOutcome.issue``."""

    severity: str
    code: str
    """The FHIR issue-type code — ``required``, ``value``, ``structure``, ``invariant``."""
    expression: str
    diagnostics: str

    def render(self) -> str:
        return f"[{self.severity}/{self.code}] {self.expression}: {self.diagnostics}"

    def to_json(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "expression": [self.expression],
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True, slots=True)
class FhirValidationOutcome:
    """The result of validating one resource."""

    issues: tuple[FhirIssue, ...] = ()
    resource_type: str = ""
    version: FhirVersion = FhirVersion.R4

    @property
    def errors(self) -> tuple[FhirIssue, ...]:
        return tuple(i for i in self.issues if i.severity in ("error", "fatal"))

    @property
    def warnings(self) -> tuple[FhirIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_operation_outcome(self) -> dict[str, Any]:
        """A real ``OperationOutcome``, which is what a FHIR client expects on a 4xx."""
        return {
            "resourceType": "OperationOutcome",
            "issue": [i.to_json() for i in self.issues]
            or [
                {
                    "severity": "information",
                    "code": "informational",
                    "diagnostics": "validation passed",
                }
            ],
        }

    def render(self) -> str:
        return "\n".join(i.render() for i in self.issues) or "no issues"


def _is_list(value: Any) -> bool:
    return isinstance(value, list)


def _primitive_ok(element_type: ElementType, value: Any) -> str:
    """Return an error message, or ``""`` when the value conforms."""
    if element_type is ElementType.BOOLEAN:
        return "" if isinstance(value, bool) else f"expected boolean, got {type(value).__name__}"
    if element_type is ElementType.INTEGER:
        # bool is a subclass of int in Python; True is not a FHIR integer.
        if isinstance(value, bool) or not isinstance(value, int):
            return f"expected integer, got {type(value).__name__}"
        return ""
    if element_type is ElementType.DECIMAL:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return f"expected decimal, got {type(value).__name__}"
        return ""
    if not isinstance(value, str):
        return f"expected {element_type.value}, got {type(value).__name__}"
    if element_type is ElementType.DATE and not _DATE.match(value):
        return f"{value!r} is not a valid date (YYYY, YYYY-MM, or YYYY-MM-DD)"
    if element_type is ElementType.DATETIME and not _DATETIME.match(value):
        return f"{value!r} is not a valid dateTime"
    if element_type is ElementType.INSTANT and not _INSTANT.match(value):
        return f"{value!r} is not a valid instant (a full timestamp with a timezone is required)"
    if element_type is ElementType.CODE and not _CODE.match(value):
        return f"{value!r} is not a valid code (no leading, trailing, or repeated whitespace)"
    if element_type is ElementType.ID and not _ID.match(value):
        return f"{value!r} is not a valid id (up to 64 characters of A-Z a-z 0-9 - .)"
    if element_type is ElementType.URI and not value.strip():
        return "uri must not be empty"
    return ""


def _coded_values(element_type: ElementType, value: Any) -> list[str]:
    """Every code a value asserts, for binding checks."""
    if element_type is ElementType.CODE:
        return [value] if isinstance(value, str) else []
    if element_type is ElementType.CODING and isinstance(value, dict):
        code = value.get("code")
        return [code] if isinstance(code, str) else []
    if element_type is ElementType.CODEABLE_CONCEPT and isinstance(value, dict):
        codes = []
        for coding in value.get("coding", []) or []:
            if isinstance(coding, dict) and isinstance(coding.get("code"), str):
                codes.append(coding["code"])
        return codes
    return []


def _check_reference(element: ElementDefinition, value: Any, expression: str) -> list[FhirIssue]:
    issues: list[FhirIssue] = []
    if not isinstance(value, dict):
        return [
            FhirIssue(
                "error",
                "structure",
                expression,
                f"expected a Reference object, got {type(value).__name__}",
            )
        ]
    literal = value.get("reference")
    if literal is None:
        if not value.get("identifier") and not value.get("display"):
            issues.append(
                FhirIssue(
                    "error",
                    "required",
                    expression,
                    "Reference has neither a literal reference, a logical identifier, nor a "
                    "display; it points at nothing",
                )
            )
        return issues
    if not isinstance(literal, str):
        return [FhirIssue("error", "structure", expression, "reference must be a string")]
    parsed = parse_reference(literal)
    if parsed is None:
        return [
            FhirIssue(
                "error",
                "value",
                expression,
                f"{literal!r} is not a typed reference; FHIR permits an untyped relative "
                "reference but this gateway does not, because the type then has to be guessed "
                "from context",
            )
        ]
    if element.reference_targets and parsed.resource_type not in element.reference_targets:
        issues.append(
            FhirIssue(
                "error",
                "value",
                expression,
                f"reference targets {parsed.resource_type}, but this element may only "
                f"reference {', '.join(sorted(element.reference_targets))}",
            )
        )
    return issues


def _check_value(element: ElementDefinition, value: Any, expression: str) -> list[FhirIssue]:
    issues: list[FhirIssue] = []

    if element.type is ElementType.REFERENCE:
        return _check_reference(element, value, expression)

    if element.type is ElementType.CODEABLE_REFERENCE:
        if not isinstance(value, dict):
            return [FhirIssue("error", "structure", expression, "expected a CodeableReference")]
        if "reference" not in value and "concept" not in value:
            return [
                FhirIssue(
                    "error",
                    "required",
                    expression,
                    "CodeableReference must carry a concept, a reference, or both",
                )
            ]
        if isinstance(value.get("reference"), dict):
            issues.extend(_check_reference(element, value["reference"], f"{expression}.reference"))
        return issues

    if element.type.is_primitive:
        if problem := _primitive_ok(element.type, value):
            issues.append(FhirIssue("error", "value", expression, problem))
    elif not isinstance(value, dict):
        issues.append(
            FhirIssue(
                "error",
                "structure",
                expression,
                f"expected {element.type.value} object, got {type(value).__name__}",
            )
        )

    if element.binding:
        codes = _coded_values(element.type, value)
        # A CodeableConcept with only text against a required binding. Warning, not error:
        # FHIR permits text-only where the code is genuinely unavailable, and rejecting it
        # would drop a documented allergy because the sender had no coded vocabulary.
        if (
            not codes
            and element.type is not ElementType.CODE
            and isinstance(value, dict)
            and value.get("text")
        ):
            issues.append(
                FhirIssue(
                    "warning",
                    "code-invalid",
                    expression,
                    "required binding satisfied by text only; no coded value present",
                )
            )
        for code in codes or ([value] if element.type is ElementType.CODE else []):
            if isinstance(code, str) and code not in element.binding:
                issues.append(
                    FhirIssue(
                        "error",
                        "code-invalid",
                        expression,
                        f"{code!r} is not in the required binding "
                        f"({', '.join(sorted(element.binding))})",
                    )
                )
    return issues


def validate_resource(
    resource: Resource, *, version: FhirVersion = FhirVersion.R4
) -> FhirValidationOutcome:
    """Validate a resource against its definition for one FHIR version."""
    definition = definition_for(resource.resource_type)
    if definition is None:
        return FhirValidationOutcome(
            issues=(
                FhirIssue(
                    "error",
                    "not-supported",
                    resource.resource_type,
                    f"resource type {resource.resource_type!r} is not served by this gateway",
                ),
            ),
            resource_type=resource.resource_type,
            version=version,
        )

    issues: list[FhirIssue] = []
    applicable = definition.for_version(version)
    by_name: dict[str, ElementDefinition] = {}
    for element in applicable:
        by_name.setdefault(element.name, element)
    all_names = {e.name for e in definition.elements}

    payload = resource.data

    modifiers = payload.get("modifierExtension")
    if isinstance(modifiers, list):
        for index, modifier in enumerate(modifiers):
            url = modifier.get("url") if isinstance(modifier, dict) else None
            if url not in _KNOWN_MODIFIER_EXTENSIONS:
                issues.append(
                    FhirIssue(
                        "error",
                        "not-supported",
                        f"{resource.resource_type}.modifierExtension[{index}]",
                        f"unrecognised modifier extension {url!r}. A modifier extension can "
                        "invert the meaning of the element it modifies, so an unrecognised one "
                        "must block processing rather than be ignored",
                    )
                )

    for name, value in payload.items():
        if name in _COMMON_ELEMENTS:
            continue
        expression = f"{resource.resource_type}.{name}"
        element = by_name.get(name)
        if element is None:
            if name in all_names:
                other = next(e for e in definition.elements if e.name == name)
                versions = ", ".join(sorted(v.value for v in other.versions))
                issues.append(
                    FhirIssue(
                        "error",
                        "structure",
                        expression,
                        f"element exists in FHIR {versions} but not in {version.value}; "
                        "writing it produces a field no client of this version reads",
                    )
                )
            else:
                issues.append(
                    FhirIssue(
                        "error",
                        "structure",
                        expression,
                        f"unknown element on {resource.resource_type}",
                    )
                )
            continue

        if element.is_repeating and not _is_list(value):
            issues.append(
                FhirIssue(
                    "error",
                    "structure",
                    expression,
                    f"element repeats (0..{element.max_cardinality}) and must be a list",
                )
            )
            continue
        if not element.is_repeating and _is_list(value):
            issues.append(
                FhirIssue(
                    "error",
                    "structure",
                    expression,
                    "element does not repeat (max 1) and must not be a list",
                )
            )
            continue

        if element.type is ElementType.BACKBONE:
            continue

        if element.is_repeating:
            if not value:
                issues.append(
                    FhirIssue(
                        "warning",
                        "structure",
                        expression,
                        "empty list; FHIR requires a repeating element to be absent rather "
                        "than present and empty",
                    )
                )
            for index, item in enumerate(value):
                issues.extend(_check_value(element, item, f"{expression}[{index}]"))
        else:
            issues.extend(_check_value(element, value, expression))

    for element in applicable:
        if element.is_required and element.name not in payload and not element.choice_of:
            issues.append(
                FhirIssue(
                    "error",
                    "required",
                    f"{resource.resource_type}.{element.name}",
                    f"required element is absent (min {element.min_cardinality})",
                )
            )

    for group, members in definition.choice_groups(version).items():
        present = [m.name for m in members if m.name in payload]
        if len(present) > 1:
            issues.append(
                FhirIssue(
                    "error",
                    "structure",
                    f"{resource.resource_type}.{group}[x]",
                    f"choice element has {len(present)} members present ({', '.join(present)}); "
                    "exactly one is permitted",
                )
            )
        elif not present and any(m.is_required for m in members):
            issues.append(
                FhirIssue(
                    "error",
                    "required",
                    f"{resource.resource_type}.{group}[x]",
                    "required choice element is absent; one of "
                    f"{', '.join(m.name for m in members)} must be present",
                )
            )

    return FhirValidationOutcome(
        issues=tuple(issues), resource_type=resource.resource_type, version=version
    )
