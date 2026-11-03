from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from daemon_core.thin_sovereign import (
    PERSONAL_RECOGNITION_THRESHOLD,
    adapt_turn_commitment_for_unfiltered_streaming,
    build_thin_sovereign_messages,
)


def test_recognition_keeps_every_source_and_changes_only_turn_threshold():
    kwargs = dict(
        message='Cela te rappelle quelque chose ?',
        recent_dialogue='',
        daemon_persona='Persona exacte',
        conversation_manner='Manière exacte',
        exact_memory_text='## Exact\nNora pratique la céramique.',
        memory_catalog='- theme/art',
        user_context='Profil inchangé',
        scene_model_text='Lecture révisable',
        host_tools_available=True,
        message_received_at=0,
    )
    reference = build_thin_sovereign_messages(**kwargs)
    assert reference == build_thin_sovereign_messages(**kwargs, recognize_shared_context=False)
    candidate = build_thin_sovereign_messages(**kwargs, recognize_shared_context=True)
    assert candidate[:-2] == reference[:-2]
    assert candidate[-1] == reference[-1]
    normalized = candidate[-2]['content'].replace(PERSONAL_RECOGNITION_THRESHOLD + '\n\n', '', 1)
    assert normalized == reference[-2]['content']
    assert candidate[-2]['content'].count(PERSONAL_RECOGNITION_THRESHOLD) == 1
    assert PERSONAL_RECOGNITION_THRESHOLD in adapt_turn_commitment_for_unfiltered_streaming(candidate[-2]['content'])
