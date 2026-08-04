"""Three-tier conversational memory.

Working memory holds recent turns verbatim, episodic memory holds summaries of turns that
have aged out, and semantic memory holds the entities the conversation is about
(docs/design/adr-0011-memory-tiers.md).

The rule that makes this safe rather than merely convenient: **memory resolves references, it
never supplies evidence.** "What about his creatinine?" uses semantic memory to turn ``his``
into a patient id — and the creatinine value is then retrieved from the record. A value
recalled from a summary is never cited, because summarisation is lossy and a lossy lab value
is a wrong lab value.

Everything is scoped to (tenant, session) at construction. A store cannot be built without a
tenant, the same constructor-level guarantee `VectorQuery` has in Phase 2.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace

from cip_core.logging import get_logger

__all__ = [
    "EntityMention",
    "MemoryStore",
    "SessionMemory",
    "Turn",
    "resolve_references",
]

_log = get_logger(__name__)

#: Pronouns and deictics that need a referent before a question can be planned.
_REFERENCE_TOKENS = re.compile(
    r"\b(?:he|she|they|him|her|them|his|hers|their|the patient|this patient|that patient)\b",
    re.I,
)

#: Follow-up openers that carry the previous question's subject forward.
_ELLIPTICAL = re.compile(r"^\s*(?:what about|how about|and|also|what's|whats|any)\b", re.I)


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange."""

    turn_id: int
    question: str
    answer: str
    asked_at: dt.datetime
    patient_id: uuid.UUID | None = None
    confidence: float = 0.0
    evidence_count: int = 0

    def token_estimate(self) -> int:
        return max(1, (len(self.question) + len(self.answer)) // 4)

    def summarise(self) -> str:
        """One-line summary for episodic memory.

        Deliberately records *what was asked and how well it was answered*, not the answer's
        content. An episodic entry is a navigational aid; letting it carry clinical values
        would make a lossy summary into a citable source.
        """
        outcome = "answered" if self.confidence >= 0.5 else "answered with low confidence"
        return f"Turn {self.turn_id}: asked '{_clip(self.question, 90)}' — {outcome}"


@dataclass(frozen=True, slots=True)
class EntityMention:
    """An entity the conversation has established."""

    kind: str
    value: str
    display: str
    last_turn: int

    def is_patient(self) -> bool:
        return self.kind == "patient"


@dataclass(frozen=True, slots=True)
class SessionMemory:
    """The three tiers for one session."""

    tenant_id: uuid.UUID
    session_id: str
    working: tuple[Turn, ...] = ()
    episodic: tuple[str, ...] = ()
    semantic: tuple[EntityMention, ...] = ()

    def current_patient(self) -> uuid.UUID | None:
        """The patient this conversation is about, if one has been established."""
        patients = [m for m in self.semantic if m.is_patient()]
        if not patients:
            return None
        latest = max(patients, key=lambda m: m.last_turn)
        try:
            return uuid.UUID(latest.value)
        except ValueError:  # pragma: no cover - written only from a validated uuid
            return None

    def last_turn(self) -> Turn | None:
        return self.working[-1] if self.working else None

    def render_context(self, *, max_turns: int = 3) -> str:
        """Recent conversation as prose, for a prompt's context section."""
        lines = list(self.episodic[-2:])
        for turn in self.working[-max_turns:]:
            lines.append(f"Q: {_clip(turn.question, 120)}")
            lines.append(f"A: {_clip(turn.answer, 200)}")
        return "\n".join(lines)


class MemoryStore:
    """Holds session memory, bounded by a token budget.

    In-process. A distributed deployment needs a shared store, but that is a persistence
    decision with PHI-retention consequences (how long does a session's conversational
    content live, and who can read it) rather than an engineering convenience — so it is
    deliberately not made here.
    """

    def __init__(
        self,
        *,
        max_working_tokens: int = 1500,
        max_working_turns: int = 6,
        max_sessions: int = 1000,
    ) -> None:
        if max_working_tokens < 1:
            raise ValueError("max_working_tokens must be >= 1")
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._max_tokens = max_working_tokens
        self._max_turns = max_working_turns
        self._max_sessions = max_sessions
        """Sessions are bounded and evicted least-recently-used. Unbounded, a long-running
        process accumulates one entry per conversation forever — an OOM, and a PHI-retention
        problem, since conversational content would live for the life of the process with no
        expiry anybody declared."""
        self._sessions: OrderedDict[tuple[uuid.UUID, str], SessionMemory] = OrderedDict()

    def _store(self, key: tuple[uuid.UUID, str], memory: SessionMemory) -> None:
        """Write a session and evict the oldest if the store is full."""
        self._sessions[key] = memory
        self._sessions.move_to_end(key)
        while len(self._sessions) > self._max_sessions:
            evicted, _ = self._sessions.popitem(last=False)
            _log.info("memory.session_evicted", session=evicted[1], reason="capacity")

    def get(self, *, tenant_id: uuid.UUID, session_id: str) -> SessionMemory:
        """Fetch or create a session's memory."""
        key = (tenant_id, session_id)
        memory = self._sessions.get(key)
        if memory is None:
            memory = SessionMemory(tenant_id=tenant_id, session_id=session_id)
            self._store(key, memory)
        else:
            self._sessions.move_to_end(key)
        return memory

    def note_entity(
        self, *, tenant_id: uuid.UUID, session_id: str, mention: EntityMention
    ) -> SessionMemory:
        """Record an entity the conversation is about."""
        memory = self.get(tenant_id=tenant_id, session_id=session_id)
        # Replace an existing mention of the same kind and value rather than appending, so a
        # long conversation about one patient does not accumulate duplicates.
        others = tuple(
            m for m in memory.semantic if not (m.kind == mention.kind and m.value == mention.value)
        )
        updated = replace(memory, semantic=(*others, mention))
        self._store((tenant_id, session_id), updated)
        return updated

    def append_turn(self, *, tenant_id: uuid.UUID, session_id: str, turn: Turn) -> SessionMemory:
        """Add a turn, evicting and summarising as the budget requires."""
        memory = self.get(tenant_id=tenant_id, session_id=session_id)
        working = [*memory.working, turn]
        episodic = list(memory.episodic)

        # Evict oldest-first on either bound. Summaries are written on eviction rather than
        # on a timer, so the transition point is deterministic and testable.
        while len(working) > self._max_turns or _tokens(working) > self._max_tokens:
            if len(working) == 1:
                break
            evicted = working.pop(0)
            episodic.append(evicted.summarise())

        updated = replace(memory, working=tuple(working), episodic=tuple(episodic))
        self._store((tenant_id, session_id), updated)
        _log.debug(
            "memory.turn_appended",
            session=session_id,
            working=len(updated.working),
            episodic=len(updated.episodic),
        )
        return updated

    def clear(self, *, tenant_id: uuid.UUID, session_id: str) -> None:
        self._sessions.pop((tenant_id, session_id), None)

    def session_count(self) -> int:
        return len(self._sessions)


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of reference resolution."""

    text: str
    patient_id: uuid.UUID | None = None
    substitutions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    """References with no referent. The planner turns these into a clarification request
    rather than proceeding against a guess."""

    changed: bool = False
    subject_carried: str | None = field(default=None)


def resolve_references(
    text: str, memory: SessionMemory, *, explicit_patient: uuid.UUID | None = None
) -> Resolution:
    """Resolve pronouns and elliptical follow-ups against session memory.

    Returns the resolved text plus what was substituted, so the trace can show the rewrite.
    An unresolvable reference is reported rather than dropped: "his creatinine" with no
    patient established must become a question, not a search for creatinine in general.
    """
    references = _REFERENCE_TOKENS.findall(text)
    patient = explicit_patient or memory.current_patient()

    substitutions: list[str] = []
    unresolved: list[str] = []
    resolved = text

    if references:
        if patient is None:
            unresolved.extend(sorted({r.lower() for r in references}))
        else:
            substitutions.append(f"{references[0].lower()} → patient {patient}")

    subject_carried: str | None = None
    if _ELLIPTICAL.match(text) and len(text.split()) <= 8:
        previous = memory.last_turn()
        if previous is not None:
            # An elliptical follow-up carries the previous *question's* subject forward, not
            # its answer. "What about his creatinine?" after "Is his potassium rising?" is a
            # trend question about creatinine — the shape comes from the earlier question.
            subject_carried = previous.question
            resolved = f"{text.rstrip('?').strip()} (following: {previous.question})"

    return Resolution(
        text=resolved,
        patient_id=patient,
        substitutions=tuple(substitutions),
        unresolved=tuple(unresolved),
        changed=resolved != text or bool(substitutions),
        subject_carried=subject_carried,
    )


def _tokens(turns: list[Turn]) -> int:
    return sum(turn.token_estimate() for turn in turns)


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
