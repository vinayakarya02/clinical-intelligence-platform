"""Tenancy and error-taxonomy tests."""

from __future__ import annotations

import uuid

import pytest

from cip_core.errors import (
    AuthorizationError,
    CipError,
    DuplicateDocumentError,
    NotFoundError,
    PipelineError,
    ProblemDetail,
    ValidationFailedError,
)
from cip_core.tenancy import (
    Role,
    TenantContext,
    current_tenant_context,
    set_current_tenant_context,
)


class TestTenantContext:
    def test_require_scope_allows_a_held_scope(self, context: TenantContext) -> None:
        context.require_scope("documents:write")

    def test_require_scope_rejects_a_missing_scope(self, context: TenantContext) -> None:
        with pytest.raises(AuthorizationError, match="documents:admin"):
            context.require_scope("documents:admin")

    def test_admin_bypasses_scope_checks(self, tenant_id: uuid.UUID) -> None:
        admin = TenantContext(tenant_id=tenant_id, actor_id="a", roles=frozenset({Role.ADMIN}))
        admin.require_scope("anything:at:all")

    def test_require_tenant_rejects_a_foreign_tenant(
        self, context: TenantContext, other_tenant_id: uuid.UUID
    ) -> None:
        with pytest.raises(AuthorizationError, match="different tenant"):
            context.require_tenant(other_tenant_id)

    def test_require_tenant_accepts_the_owning_tenant(self, context: TenantContext) -> None:
        context.require_tenant(context.tenant_id)

    def test_context_is_immutable(self, context: TenantContext) -> None:
        """A downstream layer must not be able to widen its own scope after a check."""
        with pytest.raises((AttributeError, TypeError)):
            context.tenant_id = uuid.uuid4()  # type: ignore[misc]

    def test_service_context_has_write_scope_but_not_admin(self, tenant_id: uuid.UUID) -> None:
        service = TenantContext.for_service(tenant_id)
        service.require_scope("documents:write")
        assert not service.is_admin
        assert Role.SERVICE in service.roles

    def test_ambient_context_round_trips(self, context: TenantContext) -> None:
        set_current_tenant_context(context)
        assert current_tenant_context() is context
        set_current_tenant_context(None)
        assert current_tenant_context() is None


class TestProblemDetail:
    def test_problem_includes_rfc7807_members(self) -> None:
        problem = ProblemDetail(
            type_="validation-failed", title="Bad", status=422, detail="nope", instance="/x"
        )
        assert problem["status"] == 422
        assert problem["title"] == "Bad"
        assert problem["detail"] == "nope"
        assert problem["instance"] == "/x"
        assert problem["type"].endswith("/validation-failed")


class TestErrorTaxonomy:
    @pytest.mark.parametrize(
        ("error", "expected_status"),
        [
            (NotFoundError("missing"), 404),
            (ValidationFailedError("bad"), 422),
            (AuthorizationError("no"), 403),
            (PipelineError("boom", stage="parse"), 500),
        ],
    )
    def test_errors_carry_their_http_status(self, error: CipError, expected_status: int) -> None:
        assert error.to_problem()["status"] == expected_status

    def test_validation_error_carries_violations(self) -> None:
        error = ValidationFailedError("invalid", violations=["a", "b"])
        assert error.to_problem()["violations"] == ["a", "b"]

    def test_pipeline_error_names_the_failing_stage(self) -> None:
        assert PipelineError("boom", stage="chunk").to_problem()["stage"] == "chunk"

    def test_duplicate_error_exposes_the_existing_document(self) -> None:
        existing = str(uuid.uuid4())
        error = DuplicateDocumentError("dupe", existing_document_id=existing, content_hash="a" * 64)
        problem = error.to_problem()
        assert problem["status"] == 409
        assert problem["existing_document_id"] == existing
        assert problem["content_hash"] == "a" * 64
