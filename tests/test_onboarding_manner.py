import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.onboarding import seed_conversation_manner


def _project_with_template(tmp_path) -> Path:
    template = tmp_path / "memory" / "templates" / "users" / "conversation.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        '# Way of conversing\n\nTake part in the exchange.\n',
        encoding="utf-8",
    )
    return tmp_path


def test_seed_creates_manner_for_new_user(tmp_path):
    project = _project_with_template(tmp_path)

    target = seed_conversation_manner("12345", project_root=project)

    assert target == project / "memory" / "users" / "12345" / "conversation.md"
    assert "Take part in the exchange." in target.read_text(encoding="utf-8")


def test_seed_never_touches_existing_manner(tmp_path):
    project = _project_with_template(tmp_path)
    existing = project / "memory" / "users" / "12345" / "conversation.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# Ma manière à moi\n", encoding="utf-8")

    assert seed_conversation_manner("12345", project_root=project) is None
    assert existing.read_text(encoding="utf-8") == "# Ma manière à moi\n"


def test_seed_without_template_is_a_noop(tmp_path):
    assert seed_conversation_manner("12345", project_root=tmp_path) is None


def test_shipped_manner_template_is_english():
    template = ROOT / "memory" / "templates" / "users" / "conversation.md"
    content = template.read_text(encoding="utf-8")

    assert content.startswith("# Way of conversing\n")
    assert "A GENUINE QUESTION DOES NOT CONTAIN ITS OWN ANSWER" in content
    assert "A practical request calls for directness" in content


def test_shipped_persona_leaves_name_gender_and_user_pronouns_to_identity():
    import re

    content = (ROOT / "memory" / "templates" / "users" / "persona.md").read_text(encoding="utf-8")

    assert content.startswith("Your voice is quick and sharp")
    assert "Your refusals:" in content
    assert not re.search(r"\b(?:You are [A-Z]|feminine|masculine|he|him|his|she|her)\b", content)
