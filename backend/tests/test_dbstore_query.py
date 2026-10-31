"""The Postgres ``list`` query must filter before LIMIT.

These run without a database: ``PostgresJobStore`` only connects on use, so a
store built with ``object.__new__`` can have its ``_connect`` replaced with a
recorder. The property under test is the one that silently breaks at scale — a
seller's older jobs vanishing because the filter ran after a global truncation.
"""

from __future__ import annotations

from pact.core.dbstore import PostgresJobStore


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
