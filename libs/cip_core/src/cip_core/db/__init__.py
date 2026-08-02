"""Database connectivity for the three Phase 1 stores.

Each store has a small manager class owning the client lifecycle and exposing a
``health_check()``. They are deliberately not merged into one "database" abstraction:
the three stores have genuinely different semantics (transactional relational, document,
graph), and a lowest-common-denominator wrapper would hide the differences that matter.
"""

from cip_core.db.base import Base
from cip_core.db.mongo import MongoManager
from cip_core.db.neo4j import Neo4jManager
from cip_core.db.postgres import PostgresManager

__all__ = ["Base", "MongoManager", "Neo4jManager", "PostgresManager"]
