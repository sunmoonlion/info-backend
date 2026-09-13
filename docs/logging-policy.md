# Shared SQL / HTTP logging policy

API initializes the application console logger. Worker and Scheduler keep
Celery's own handlers, formatting, CLI log level and task context; an
`after_setup_logger` receiver applies the same library policy afterwards.
Direct imports of `app.worker` register this hook too. It does not intercept
Celery's `setup_logging` signal or start a broker connection.

`sqlalchemy.engine`, `sqlalchemy.pool`, `httpx` and `httpcore` use WARNING in all
environments, including application DEBUG. Existing explicitly configured child
loggers are reset to WARNING when the policy is applied; future ordinary children
inherit it. Application/audit/Celery levels are unchanged. Later external logger
reconfiguration can override this policy and is not covered by this guarantee.

The shared Postgres engine always uses `echo=False, hide_parameters=True`.
Development no longer implicitly prints SQL and parameter values. Alembic remains
independent: its existing config uses SQLAlchemy WARN / Alembic INFO without echo.

This is **noise reduction, not complete redaction**. WARNING/ERROR remain visible;
SQL literals, application exception messages, request/access logs and other SDKs
may still contain sensitive content. Do not log credentials, provider payloads or
presigned URLs. SQL/HTTP wire debugging requires a separately controlled diagnostic
session with appropriate data and log access/retention, not merely `LOG_LEVEL=DEBUG`.
No old logs are removed and no retention/archival policy is added here.

`tests/test_logging_policy.py` exercises real API/Celery bootstrap logging in
isolated processes, INFO/DEBUG, reinitialization, real httpx emission with an
in-memory transport, and shared Postgres engine flags/exception rendering.
These tests are not deployment, monitoring, alert delivery or privacy certification.
