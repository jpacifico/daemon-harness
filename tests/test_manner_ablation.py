import hashlib

import pytest

from scripts.replay_context_availability import project_manner


MANNER = (
    "# Manner\n\nKeep questions natural.\n\nAccept a short response.\n\nEnd simply."
)
SPEC = {
    "text_sha256": hashlib.sha256(MANNER.encode()).hexdigest(),
    "paragraph_index": 2,
    "paragraph_sha256": hashlib.sha256(b"Accept a short response.").hexdigest(),
}


def test_no_ablation_preserves_arbitrary_text_without_manifest():
    assert project_manner(MANNER, None, "none") == MANNER


def test_paragraph_ablation_removes_only_registered_span():
    assert project_manner(MANNER, SPEC, "omit-paragraph") == MANNER.replace(
        "Accept a short response.\n\n", "", 1
    )


def test_full_ablation_is_empty_after_source_verification():
    assert project_manner(MANNER, SPEC, "omit-manner") == ""


@pytest.mark.parametrize("arm", ["omit-paragraph", "omit-manner"])
def test_changed_source_rejected_before_ablation(arm):
    with pytest.raises(AssertionError):
        project_manner(MANNER + "Changed.", SPEC, arm)


def test_changed_selection_rejected():
    with pytest.raises(AssertionError):
        project_manner(MANNER, {**SPEC, "paragraph_index": 1}, "omit-paragraph")


def test_unknown_operation_rejected():
    with pytest.raises(ValueError):
        project_manner(MANNER, SPEC, "unknown")
