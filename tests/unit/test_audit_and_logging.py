"""Audit hash-chain and log-redaction tests.

Both are compliance controls, so the tests assert the control actually holds rather than
that the function runs: the chain must *detect* tampering, and redaction must survive a
developer logging PHI by accident.
"""

from __future__ import annotations

import datetime as dt
import uuid

from cip_core.audit import (
    GENESIS_HASH,
    AuditRecord,
    canonical_payload,
    compute_row_hash,
    verify_chain,
)
from cip_core.logging import REDACTED, redact_sensitive


def _record(action: str = "document.ingested", **overrides: object) -> AuditRecord:
    base: dict[str, object] = {
        "tenant_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "action": action,
        "resource_type": "document",
        "resource_id": "doc-1",
        "occurred_at": dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.UTC),
    }
    base.update(overrides)
    return AuditRecord(**base)  # type: ignore[arg-type]


def _build_chain(records: list[AuditRecord]) -> list[tuple[AuditRecord, str, str]]:
    entries: list[tuple[AuditRecord, str, str]] = []
    prev = GENESIS_HASH
    for record in records:
        row_hash = compute_row_hash(record, prev)
        entries.append((record, prev, row_hash))
        prev = row_hash
    return entries


class TestCanonicalPayload:
    def test_encoding_is_deterministic(self) -> None:
        record = _record()
        assert canonical_payload(record, GENESIS_HASH) == canonical_payload(record, GENESIS_HASH)

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        """A caller that forgets tzinfo must still produce a verifiable chain."""
        naive = _record(occurred_at=dt.datetime(2026, 3, 14, 12, 0))
        aware = _record(occurred_at=dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.UTC))
        assert compute_row_hash(naive, GENESIS_HASH) == compute_row_hash(aware, GENESIS_HASH)

    def test_equivalent_timestamps_in_other_zones_hash_identically(self) -> None:
        utc = _record(occurred_at=dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.UTC))
        offset = _record(
            occurred_at=dt.datetime(2026, 3, 14, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        )
        assert compute_row_hash(utc, GENESIS_HASH) == compute_row_hash(offset, GENESIS_HASH)

    def test_differing_records_hash_differently(self) -> None:
        a = compute_row_hash(_record("document.ingested"), GENESIS_HASH)
        b = compute_row_hash(_record("document.deleted"), GENESIS_HASH)
        assert a != b


class TestChainVerification:
    def test_intact_chain_reports_no_failures(self) -> None:
        entries = _build_chain([_record(f"action.{i}") for i in range(5)])
        assert verify_chain(entries) == []

    def test_altering_a_row_is_detected(self) -> None:
        records = [_record(f"action.{i}") for i in range(5)]
        entries = _build_chain(records)
        _original, prev_hash, row_hash = entries[2]
        entries[2] = (_record("action.TAMPERED"), prev_hash, row_hash)
        assert 2 in verify_chain(entries)

    def test_deleting_a_row_is_detected(self) -> None:
        entries = _build_chain([_record(f"action.{i}") for i in range(5)])
        del entries[2]
        # Removing a row breaks the prev_hash linkage of everything after it.
        assert verify_chain(entries), "a deleted row must not go unnoticed"

    def test_reordering_rows_is_detected(self) -> None:
        entries = _build_chain([_record(f"action.{i}") for i in range(4)])
        entries[1], entries[2] = entries[2], entries[1]
        assert verify_chain(entries)

    def test_all_damaged_indices_are_reported(self) -> None:
        """Verification reports the full extent of damage, not just the first bad row."""
        records = [_record(f"action.{i}") for i in range(6)]
        entries = _build_chain(records)
        entries[1] = (_record("tampered.1"), entries[1][1], entries[1][2])
        entries[4] = (_record("tampered.4"), entries[4][1], entries[4][2])
        failures = verify_chain(entries)
        assert 1 in failures
        assert 4 in failures

    def test_empty_chain_is_valid(self) -> None:
        assert verify_chain([]) == []


class TestLogRedaction:
    def test_sensitive_keys_are_redacted(self) -> None:
        event = {"chunk_text": "Patient has diabetes", "document_id": "doc-1"}
        result = redact_sensitive(None, "info", event)
        assert result["chunk_text"] == REDACTED
        assert result["document_id"] == "doc-1"

    def test_nested_dictionaries_are_redacted(self) -> None:
        event = {"result": {"text": "PHI here", "count": 3}}
        result = redact_sensitive(None, "info", event)
        assert result["result"]["text"] == REDACTED
        assert result["result"]["count"] == 3

    def test_compound_key_names_are_matched(self) -> None:
        event = {"patient_name": "Jordan Rivera", "api_key": "sk-123", "raw_payload": "x"}
        result = redact_sensitive(None, "info", event)
        assert all(result[key] == REDACTED for key in event)

    def test_matching_is_case_insensitive(self) -> None:
        result = redact_sensitive(None, "info", {"Authorization": "Bearer abc"})
        assert result["Authorization"] == REDACTED

    def test_innocuous_keys_are_untouched(self) -> None:
        event = {"document_id": "d", "chunk_count": 4, "duration_ms": 12.5}
        assert redact_sensitive(None, "info", dict(event)) == event
