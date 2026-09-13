"""One immutable not-before predicate for publisher and consumer paths.

All clocks are PostgreSQL clocks. Never use available_at to gate consumption:
finish_delivery advances it for transport backoff even on successful publication.
Missing metadata preserves immediate legacy events; malformed metadata fails closed.
"""

from app.application.dto.outbox import NOT_BEFORE_HEADER

NOT_BEFORE_DUE_SQL = (
    f"COALESCE((headers->>'{NOT_BEFORE_HEADER}')::timestamptz, "
    "'-infinity'::timestamptz) <= clock_timestamp()"
)
