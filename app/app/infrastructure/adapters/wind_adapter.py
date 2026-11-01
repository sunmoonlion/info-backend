"""Re-export of WindAdapter for infrastructure.adapters."""
from app.infrastructure.external.wind_adapter import (
    CHART_TO_WIND,
    REPLAY_SNAPSHOTS,
    WIND_TO_CHART,
    CircuitBreaker,
    DegradationCode,
    WindAdapter,
    WindAuthError,
    WindCredential,
    WindError,
    WindRateLimitError,
    safe_fetch_with_degradation,
)

__all__ = [
    "CHART_TO_WIND",
    "REPLAY_SNAPSHOTS",
    "WIND_TO_CHART",
    "CircuitBreaker",
    "DegradationCode",
    "WindAdapter",
    "WindAuthError",
    "WindCredential",
    "WindError",
    "WindRateLimitError",
    "safe_fetch_with_degradation",
]
