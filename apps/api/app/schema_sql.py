"""Loader for the canonical schema.

``packages/contracts/schema.sql`` wraps itself in BEGIN/COMMIT so it can be run
straight through psql. Alembic and the test harness both manage their own
transaction, so those two statements are stripped here rather than duplicated
out of the file — the SQL stays runnable by hand, which is how people actually
inspect it.
"""

from __future__ import annotations

import re

from .config import SCHEMA_SQL

_TXN = re.compile(r"^\s*(BEGIN|COMMIT)\s*;\s*$", re.IGNORECASE | re.MULTILINE)


def load_schema_sql(path=None) -> str:
    sql = (path or SCHEMA_SQL).read_text()
    return _TXN.sub("", sql)
