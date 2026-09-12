"""Frozen, bounded keyset scan for the 0008 identity migration and read-only CLI."""

from sqlalchemy import text


def iter_identity_rows_v1(connection):
    after = None
    while True:
        query = "SELECT id,canonical_url FROM info_document "
        if after is not None:
            query += "WHERE id>:after "
        rows = connection.execute(
            text(query + "ORDER BY id LIMIT 500"), {"after": after}
        ).all()
        if not rows:
            return
        yield from rows
        after = rows[-1].id
