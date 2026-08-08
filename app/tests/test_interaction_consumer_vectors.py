from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.application.dto.interaction import (
    RUN_EVENT_ADAPTER,
    RunAction,
    RunSnapshot,
)


def vectors() -> dict[str, object]:
    source = os.environ.get("WEB_INTERACTION_CONSUMER_VECTORS")
    if not source:
        pytest.skip("cross-repository consumer vectors were not requested")
    return json.loads(Path(source).read_text(encoding="utf-8"))


def test_shared_web_interaction_v1_valid_vectors() -> None:
    contract = vectors()
    assert contract["contract"] == "sunmoonai.web-interaction"
    assert contract["contract_version"] == 1
    valid = contract["valid"]
    for value in valid["snapshots"]:  # type: ignore[index]
        RunSnapshot.model_validate(value)
    for value in valid["events"]:  # type: ignore[index]
        RUN_EVENT_ADAPTER.validate_python(value)
    for value in valid["actions"]:  # type: ignore[index]
        RunAction.model_validate(value)


def test_shared_web_interaction_v1_invalid_vectors() -> None:
    invalid = vectors()["invalid"]
    for vector in invalid["snapshots"]:  # type: ignore[index]
        with pytest.raises(ValidationError):
            RunSnapshot.model_validate(vector["value"])
    for vector in invalid["events"]:  # type: ignore[index]
        with pytest.raises(ValidationError):
            RUN_EVENT_ADAPTER.validate_python(vector["value"])
    for vector in invalid["actions"]:  # type: ignore[index]
        with pytest.raises(ValidationError):
            RunAction.model_validate(vector["value"])
