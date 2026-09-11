"""Adopt template delivery and move the former Info outbox into archival storage."""
from alembic import op
from app.infrastructure.messaging import delivery_schema

revision = "20260911_0007"
down_revision = "20260811_0006"
branch_labels = None
depends_on = None


def upgrade():
    delivery_schema.upgrade()
    # Preserve message identity and completion receipts; reset transport attempts
    # at the explicit migration boundary. The complete old journal remains below.
    op.execute("""
        INSERT INTO outbox_message(id,topic,aggregate_key,deduplication_key,payload,
            status,available_at,created_at,updated_at,published_at)
        SELECT id,topic,aggregate_id::text,idempotency_key,payload,
            CASE WHEN state='completed' THEN 'published' ELSE 'pending' END,
            clock_timestamp(),created_at,clock_timestamp(),published_at
        FROM delivery_outbox_message
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO inbox_message(consumer,message_id)
        SELECT topic,id FROM delivery_outbox_message WHERE state='completed'
        ON CONFLICT DO NOTHING
    """)
    op.execute("ALTER TABLE delivery_outbox_message RENAME TO delivery_outbox_message_legacy")
    # Running crawls are known execution requests. Historic pending rows carry no
    # durable enqueue flag and may be intentionally manual: never guess consent.
    op.execute("""
        INSERT INTO outbox_message(id,topic,aggregate_key,deduplication_key,payload)
        SELECT gen_random_uuid(),'info.crawl.v1',id::text,
            concat_ws(':','info.crawl',id,'v1','0'),jsonb_build_object('job_id',id::text)
        FROM crawl_job WHERE status='running'
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        UPDATE crawl_job SET request=COALESCE(request,'{}'::jsonb)||jsonb_build_object('enqueue',true)
        WHERE status='running'
    """)
    # Search is derived: rebuilding every retained version repairs missing old hints.
    op.execute("""
        INSERT INTO outbox_message(id,topic,aggregate_key,deduplication_key,payload)
        SELECT gen_random_uuid(),'info.index.v1',id::text,
            concat_ws(':','info.index',id,'v1'),jsonb_build_object('document_version_id',id::text)
        FROM info_document_version
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    # Rollback requires the documented drain/backup procedure. Never silently
    # throw away newly accepted commands when an operator runs downgrade alone.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM outbox_message m
              WHERE m.topic IN ('info.crawl.v1','info.index.v1','info.distribution.dispatch.v1')
              AND NOT EXISTS(SELECT 1 FROM inbox_message i
                  WHERE i.consumer=m.topic AND i.message_id=m.id)) THEN
            RAISE EXCEPTION 'drain or restore backup before delivery downgrade';
          END IF;
        END $$
    """)
    op.execute("""
        UPDATE delivery_outbox_message_legacy d SET state='completed',
            completed_at=COALESCE(completed_at,clock_timestamp())
        WHERE EXISTS(SELECT 1 FROM inbox_message i
            WHERE i.consumer=d.topic AND i.message_id=d.id)
    """)
    op.execute("ALTER TABLE delivery_outbox_message_legacy RENAME TO delivery_outbox_message")
    delivery_schema.downgrade()
