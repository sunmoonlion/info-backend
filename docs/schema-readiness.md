# API schema readiness

All three ready aliases (`/health/ready`, `/ready`, `/api/health`) require Redis
ping and exactly one database `alembic_version` row matching the sole packaged
migration head. Expected revision comes from the image's Alembic scripts, not a
second environment setting. Missing/empty/old/ahead/multi-row versions, broken
packaging, insufficient SELECT permission and dependency failures return the
existing generic 503 response. Database results are checked anew on every probe.

The check never runs Alembic env.py, upgrades, stamps, creates a table or grants
access. API uses its existing database principal and migration search path; the
principal must be permitted to SELECT the version table. Deployment must verify
that permission separately without exposing or borrowing migration credentials.

One cooperative 2-second asyncio timeout covers Redis and the DB check. Drivers
must honor cancellation; this is not a hard process-kill deadline. Cancellation
from the caller propagates. Liveness (`/health/live`, `/health`) stays independent
of external dependencies. Public responses do not expose revisions or URLs;
failure logs contain exception type only.

Exact equality intentionally rejects unapproved cross-version compatibility.
Rollout/rollback must coordinate code and database revisions: do not blindly run
older code against a newer schema or remove the check to make rollout green.
This is a readiness routing signal, not request middleware, full schema/data
integrity verification, API identity validation or a deployment acceptance.
Stamping the version alone cannot prove tables, constraints or permissions exist.

Worker consumer/queue checks, Scheduler liveness, migration rollout policy,
metrics/alerts and real-environment restore/drain remain separate requirements.
