import pytest

from benchmarks.useful_continuity.corpus import CASES
from benchmarks.useful_continuity.review import distribution, validate_review
from benchmarks.useful_continuity.run import internal_scope


def test_frozen_case_allocation_and_natural_messages():
    assert len(CASES) == len({c.id for c in CASES}) == 12
    assert sum(c.control for c in CASES) == 4
    assert len({c.family for c in CASES}) == 4
    assert all(len(c.history) == 2 and len(c.criteria) == 3 for c in CASES)
    for family in {c.family for c in CASES}:
        group = [c for c in CASES if c.family == family]
        assert len(group) == 3 and sum(c.control for c in group) == 1


def reviewed(passed=True, quote="The cover first."):
    return [{"id": "r1", "checks": [
        {"criterion": i, "passed": passed, "quote": quote, "reason": "Evidence supports this criterion."}
        for i in range(3)], "redundant_questions": 0}]


def test_review_rejects_fabricated_evidence_and_incomplete_criteria():
    source = [{"id": "r1", "response": "The cover first."}]
    assert validate_review(source, reviewed()) == {"r1": True}
    assert validate_review(source, reviewed(False)) == {"r1": False}
    with pytest.raises(ValueError, match="exact quotation"):
        validate_review(source, reviewed(quote="An invented sentence."))
    broken = reviewed()
    broken[0]["checks"].pop()
    with pytest.raises(ValueError, match="three preregistered"):
        validate_review(source, broken)


def test_missing_response_cannot_pass_and_missing_review_is_rejected():
    with pytest.raises(ValueError, match="absent response"):
        validate_review([{"id": "r1", "response": ""}], reviewed())
    with pytest.raises(ValueError, match="exactly once"):
        validate_review([{"id": "r1", "response": "The cover first."}], [])


def test_partial_provider_usage_cannot_be_reported_as_complete():
    events = [{"event": "llm_call", "phase": "after_turn", "provider_usage": {"total_tokens": 10}},
              {"event": "llm_call", "phase": "after_turn", "provider_usage": None}]
    critical, background = internal_scope(events, "C")
    assert critical["total"]["value"] == 0
    assert background["call_count"] == 2
    assert background["total"] == {"value": None, "quality": "unavailable"}


def test_latency_distribution_preserves_missing_values_and_tail():
    assert distribution([None, 1, 2, 100]) == {
        "measured": 3, "unavailable": 1, "min": 1, "median": 2,
        "p90_nearest_rank": 100, "max": 100,
    }
    assert distribution([None])["median"] is None
