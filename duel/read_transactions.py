"""Consistent projections without reserving SQLite's single writer slot."""
from contextlib import contextmanager

from django.db import DEFAULT_DB_ALIAS, connections, transaction


@contextmanager
def snapshot_transaction(using=DEFAULT_DB_ALIAS):
    db = connections[using]
    db.ensure_connection()
    # Reuse a command's IMMEDIATE transaction. PostgreSQL keeps its existing
    # atomic/select_for_update policy. Only standalone SQLite reads differ.
    change_mode = db.vendor == 'sqlite' and not db.in_atomic_block
    original = db.transaction_mode if change_mode else None
    if change_mode:
        db.transaction_mode = 'DEFERRED'
    try:
        with transaction.atomic(using=using):
            yield
    finally:
        if change_mode:
            db.transaction_mode = original
