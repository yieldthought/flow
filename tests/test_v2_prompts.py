from __future__ import annotations

import json

import pytest

from flow.v2.prompts import Decision, parse_decision


def test_parse_decision_needs_only_choice_and_reason() -> None:
    assert parse_decision('{"choice": " done ", "reason": " work complete "}') == Decision(
        choice="done", reason="work complete"
    )


def test_parse_decision_ignores_legacy_echoed_request_id() -> None:
    assert parse_decision(
        '{"request_id": "copied-incorrectly", "choice": "done", "reason": "complete"}'
    ) == Decision(choice="done", reason="complete")


@pytest.mark.parametrize("response", ["not json", "[]", "null"])
def test_parse_decision_rejects_invalid_json_or_non_object(response: str) -> None:
    with pytest.raises(ValueError, match="invalid transition JSON|must be a JSON object"):
        parse_decision(response)


@pytest.mark.parametrize("field", ["choice", "reason"])
@pytest.mark.parametrize("value", [None, "", " \n ", 123, True, [], {}])
def test_parse_decision_requires_non_empty_strings(field: str, value: object) -> None:
    payload = {"choice": "done", "reason": "complete", field: value}
    with pytest.raises(ValueError, match="non-empty choice and reason strings"):
        parse_decision(json.dumps(payload))


@pytest.mark.parametrize("payload", [{}, {"choice": "done"}, {"reason": "complete"}])
def test_parse_decision_requires_both_fields(payload: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="non-empty choice and reason strings"):
        parse_decision(json.dumps(payload))
