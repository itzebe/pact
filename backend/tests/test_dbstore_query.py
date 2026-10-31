"""The Postgres ``list`` query must filter before LIMIT.

These run without a database: ``PostgresJobStore`` only connects on use, so a
store built with ``object.__new__`` can have its ``_connect`` replaced with a
recorder. The property under test is the one that silently breaks at scale — a
seller's older jobs vanishing because the filter ran after a global truncation.
"""

from __future__ import annotations

import re

from pact.core.dbstore import MIGRATIONS, SCHEMA, PostgresJobStore


def _statements(sql: str) -> list[str]:
    """Split a schema blob into individual statements (comments stripped)."""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


# Columns that only MIGRATIONS adds to a database created by the *original*
# release. A statement that references one of these must not run before the ALTER
# that creates it.
_LATE_COLUMNS = ("buyer_id", "deleted_at", "run_lease_expires_at", "approval_claimed_at")


def test_schema_never_references_a_column_that_only_a_migration_adds():
    """An index on a not-yet-added column makes startup fail before the migration.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing database, so the new
    columns arrive via ALTER in MIGRATIONS. If SCHEMA created an index over one of
    them, an already-deployed database would raise ``UndefinedColumn`` at startup —
    before the ALTER that adds the column ever ran, i.e. the migration would be dead
    code on exactly the databases it exists for.
    """
    for statement in _statements(SCHEMA):
        for column in _LATE_COLUMNS:
            assert column not in statement, (
                f"SCHEMA references late column {column!r} before MIGRATIONS adds it: "
                f"{statement!r}"
            )


def test_each_late_column_is_added_before_the_index_that_uses_it():
    seen_columns: set[str] = set()
    statements = list(MIGRATIONS)
    for statement in statements:
        upper = statement.upper()
        if upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper:
            for column in _LATE_COLUMNS:
                if f" {column} " in statement or statement.rstrip().endswith(column):
                    seen_columns.add(column)
        if upper.startswith("CREATE INDEX"):
            for column in _LATE_COLUMNS:
                if column in statement:
                    assert column in seen_columns, (
                        f"index on {column!r} is declared before the ALTER that adds it"
                    )


def test_the_seller_filter_indexes_are_present():
    joined = " ".join(MIGRATIONS)
    assert "jobs_seller_id_idx" in joined
    assert "jobs_doc_gin_idx" in joined and "jsonb_path_ops" in joined


class _Recorder:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        recorder = self

        class _Cursor:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, sql, params=None):
                recorder.sql = sql
                recorder.params = tuple(params or ())

            def fetchall(self_inner):
                return []

        return _Cursor()


def _store_with_recorder() -> tuple[PostgresJobStore, _Recorder]:
    store = object.__new__(PostgresJobStore)
    recorder = _Recorder()
    store._connect = lambda: recorder  # type: ignore[method-assign]
    return store, recorder


def test_seller_filter_is_in_sql_not_applied_after_the_limit():
    store, rec = _store_with_recorder()
    store.list(limit=5, seller_id="seller_nova")

    assert "LIMIT %s" in rec.sql
    # The seller predicate must be part of the WHERE, and must cover both the
    # winning seller and an invited seller, so it cannot be a post-filter.
    assert "doc ->> 'seller_id' = %s" in rec.sql
    assert "doc -> 'offers' @> %s::jsonb" in rec.sql
    assert rec.params == ("seller_nova", '[{"seller_id": "seller_nova"}]', 5)


def test_buyer_filter_still_works_and_composes_with_seller():
    store, rec = _store_with_recorder()
    store.list(limit=10, buyer_id="buyer_a")
    assert "buyer_id = %s" in rec.sql
    assert rec.params == ("buyer_a", 10)

    store, rec = _store_with_recorder()
    store.list(limit=10, buyer_id="buyer_a", seller_id="seller_nova")
    assert "buyer_id = %s" in rec.sql and "doc ->> 'seller_id' = %s" in rec.sql
    assert rec.params == ("buyer_a", "seller_nova", '[{"seller_id": "seller_nova"}]', 10)


def test_no_filter_selects_all_non_deleted_jobs():
    store, rec = _store_with_recorder()
    store.list(limit=50)
    assert "deleted_at IS NULL" in rec.sql
    assert rec.params == (50,)
