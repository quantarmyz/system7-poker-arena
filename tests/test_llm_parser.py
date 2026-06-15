"""Verifies _parse_action_json handles the realistic case where the JSON's
`reasoning` field contains a YAML flow expression with inner braces.

The old `rfind("{")` parser broke on this and silently fell back to L1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))

import llm_agent  # noqa: E402


PAYLOAD_WITH_INNER_BRACES = (
    '{"action":"call","message":"ok",'
    '"reasoning":"{vr: \\"ln:JJ+\\", ke: \\"40% eq\\", '
    'bf: [], pp: \\"IP call\\", sr: \\"price\\"}"}'
)


def test_parse_action_with_yaml_reasoning():
    obj = llm_agent._parse_action_json(PAYLOAD_WITH_INNER_BRACES)
    assert obj is not None, "parser dropped a valid JSON object"
    assert obj["action"] == "call"
    assert obj["message"] == "ok"
    assert obj["reasoning"].startswith("{vr: ")
    assert obj["reasoning"].endswith("}")
    assert "ln:JJ+" in obj["reasoning"]


def test_parse_action_with_code_fence():
    fenced = (
        "```json\n"
        + PAYLOAD_WITH_INNER_BRACES
        + "\n```"
    )
    obj = llm_agent._parse_action_json(fenced)
    assert obj is not None
    assert obj["action"] == "call"


def test_parse_action_with_prose_around_json():
    noisy = (
        "Here is my decision.\n"
        + PAYLOAD_WITH_INNER_BRACES
        + "\nThanks."
    )
    obj = llm_agent._parse_action_json(noisy)
    assert obj is not None
    assert obj["action"] == "call"


def test_parse_action_missing_keys_returns_none():
    assert llm_agent._parse_action_json('{"action":"call"}') is None


def test_parse_action_non_object_returns_none():
    assert llm_agent._parse_action_json('["call"]') is None
    assert llm_agent._parse_action_json('not json') is None
