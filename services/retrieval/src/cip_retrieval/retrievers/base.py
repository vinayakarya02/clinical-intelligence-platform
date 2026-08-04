"""Retriever contract.

Every retriever takes a :class:`RetrievalQuery` and returns ranked
:class:`RetrievalCandidate` values with its own strategy's rank and score already recorded.
Recording the rank inside the retriever — rather than having fusion infer it from list
position — keeps the two concerns separate: a retriever knows its own ordering semantics,
and fusion only needs ranks.

Retrievers do not filter by tenant as a courtesy; they push the tenant into their backing
store's query. A retriever that returned another tenant's data would be a PHI incident, and
the stores enforce that (see :mod:`cip_retrieval.vectorstore.base`).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cip_retrieval.domain import RetrievalCandidate, RetrievalQuery, RetrievalStrategy

__all__ = ["Retriever"]


@runtime_checkable
class Retriever(Protocol):
    """Produces ranked candidates for a query."""

    @property
    def strategy(self) -> RetrievalStrategy:
        """Which strategy this retriever implements, used to key fusion weights."""
        ...

    async def retrieve(self, query: RetrievalQuery, *, limit: int) -> list[RetrievalCandidate]:
        """Return up to ``limit`` candidates, best first.

        ``limit`` is separate from :attr:`RetrievalQuery.top_k`: fusion needs a deeper pool
        from each strategy than the caller ultimately wants, because a document ranked 15th
        by vectors and 2nd by keywords should still surface.
        """
        ...
