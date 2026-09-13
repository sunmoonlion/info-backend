"""Frozen 0009 preflight: bounded, read-only, no payloads or dataset names leaked."""

from sqlalchemy import text


def audit_distribution_identity_v1(connection) -> dict:
    rows = (
        connection.execute(
            text("""
        WITH conflicts AS (
            SELECT min(id::text) AS sample_id, count(*) AS records
            FROM distribution_record
            GROUP BY document_version_id, target_app,
                     coalesce(nullif(target_dataset, ''), 'default')
            HAVING count(*) > 1
        )
        SELECT sample_id, records, count(*) OVER () AS groups,
               sum(records) OVER () AS conflicting_records
        FROM conflicts ORDER BY sample_id LIMIT 50
    """)
        )
        .mappings()
        .all()
    )
    return {
        "protocol": "distribution-identity-v1",
        "ready": not rows,
        "total_records": connection.scalar(
            text("SELECT count(*) FROM distribution_record")
        ),
        "conflicting_groups": int(rows[0]["groups"]) if rows else 0,
        "conflicting_records": int(rows[0]["conflicting_records"]) if rows else 0,
        "samples": [
            {"record_id": row["sample_id"], "records": int(row["records"])}
            for row in rows
        ],
    }
