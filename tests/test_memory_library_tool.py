import json
import subprocess
import sys
from pathlib import Path

from daemon_core.memory.graph import build_memory_graph, write_memory_graph_index


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL = PROJECT_ROOT / "scripts" / "memory_library.py"


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _event(result: subprocess.CompletedProcess[str]) -> dict:
    prefix = "DAEMON_LIBRARY_EVENT "
    line = next(line for line in result.stdout.splitlines() if line.startswith(prefix))
    return json.loads(line[len(prefix) :])


def test_read_returns_exact_bodies_and_a_digest_ledger(tmp_path):
    root = tmp_path / "library"
    souvenir = root / "theme" / "famille" / "gouts.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text("# Gouts\n\n- Sushi.\n", encoding="utf-8")

    result = _run(
        root,
        "read",
        "--established",
        "Sam est concerné.",
        "--uncertain",
        "Les autres participants restent inconnus.",
        "--need",
        "Connaître ses goûts.",
        "--handling-mode",
        "clarify",
        "--handling-plan",
        "Qui d'autre est concerné ?",
        "theme/famille/gouts",
    )

    assert result.returncode == 0
    event = _event(result)
    assert event["read_paths"] == ["theme/famille/gouts"]
    assert event["scene_assessment"]["uncertain"] == [
        "Les autres participants restent inconnus."
    ]
    assert event["scene_assessment"]["handling"]["mode"] == "clarify"
    assert len(event["sha256"]["theme/famille/gouts"]) == 64
    assert "# [[theme/famille/gouts]]" in result.stdout
    assert "- Sushi." in result.stdout
    assert "Qui d'autre est concerné ?" in result.stdout


def test_read_rejects_paths_outside_the_view(tmp_path):
    root = tmp_path / "library"
    root.mkdir()

    result = _run(
        root,
        "read",
        "--need",
        "Chercher le fait exact.",
        "--handling-mode",
        "direct",
        "--handling-plan",
        "Répondre directement si la souvenir existe.",
        "../secret",
    )

    assert result.returncode == 0
    assert _event(result)["rejected_paths"] == ["../secret"]


def test_read_acquires_exact_body_before_rejudging_provisional_handling(tmp_path):
    root = tmp_path / "library"
    souvenir = root / "theme" / "famille" / "composition.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text("# Composition\n\n- Fait exact.\n", encoding="utf-8")

    result = _run(
        root,
        "read",
        "--uncertain",
        "La composition reste à établir.",
        "--need",
        "Retrouver la composition exacte.",
        "--handling-mode",
        "direct",
        "--handling-plan",
        "Répondre si la lecture résout l'inconnue.",
        "theme/famille/composition",
    )

    assert result.returncode == 0
    assert _event(result)["read_paths"] == ["theme/famille/composition"]
    assert "- Fait exact." in result.stdout
    assert 'proposed before reading does not resolve' in result.stdout


def test_offer_returns_the_exact_model_authored_invitation(tmp_path):
    root = tmp_path / "library"
    souvenir = root / "theme" / "mondes.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text("# Mondes\n\n- Figurines et décors.\n", encoding="utf-8")
    plan = "J'ai une piste sur les mondes miniatures : ça t'intéresse ?"

    result = _run(
        root,
        "read",
        "--established",
        "Le sujet précédent est clos.",
        "--need",
        "Trouver un autre terrain concret.",
        "--handling-mode",
        "offer",
        "--handling-plan",
        plan,
        "theme/mondes",
    )

    assert result.returncode == 0
    assert _event(result)["scene_assessment"]["handling"]["mode"] == "offer"
    assert plan in result.stdout
    assert 'Now respond with exactly' in result.stdout
    assert 'Add no elaboration' in result.stdout


def test_neighbors_only_returns_explicit_links_present_in_view(tmp_path):
    root = tmp_path / "library"
    source = root / "theme" / "repas.md"
    linked = root / "theme" / "gouts_sam.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "## Liens\n- theme/gouts_sam : exact\n- theme/absent : non disponible\n",
        encoding="utf-8",
    )
    linked.write_text("# Sam\n", encoding="utf-8")

    result = _run(root, "neighbors", "theme/repas")

    assert result.returncode == 0
    assert _event(result)["paths"] == ["theme/gouts_sam"]


def test_browse_is_bounded_to_the_requested_subtree(tmp_path):
    root = tmp_path / "library"
    (root / "theme" / "famille").mkdir(parents=True)
    (root / "theme" / "loisirs").mkdir(parents=True)
    (root / "theme" / "famille" / "a.md").write_text("A", encoding="utf-8")
    (root / "theme" / "loisirs" / "b.md").write_text("B", encoding="utf-8")

    result = _run(root, "browse", "theme/famille")

    assert result.returncode == 0
    assert _event(result)["paths"] == ["theme/famille/a"]


def test_members_returns_bounded_backlinks_without_reading_member_bodies(tmp_path):
    memory_root = tmp_path / "memory"
    root = memory_root / "capsules"
    center = root / "theme" / "episode.md"
    member = root / "theme" / "member.md"
    l0 = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / "example.md"
    )
    center.parent.mkdir(parents=True)
    l0.parent.mkdir(parents=True)
    center.write_text(
        "---\ncollection:\n  coverage: open\n---\n# Episode\n",
        encoding="utf-8",
    )
    member.write_text(
        "# Member secret\n\n## Relations\n"
        "- member_of [[theme/episode]] : souvenir de scene\n"
        "  - source: [[levels/L0_ground_zero/conversations/example#turn]]\n",
        encoding="utf-8",
    )
    l0.write_text("# L0\n\n## turn\n", encoding="utf-8")
    build = build_memory_graph(root, memory_root=memory_root)
    index_path = memory_root / "levels" / "memory_graph.json"
    write_memory_graph_index(index_path, build.index)

    result = _run(
        root,
        "members",
        "theme/episode",
        "--graph-index",
        str(index_path),
        "--memory-root",
        str(memory_root),
    )

    assert result.returncode == 0
    event = _event(result)
    assert event["members"] == [
        {"path": "theme/member", "role": "souvenir de scene"}
    ]
    assert event["coverage"] == "open"
    assert event["claimed_exhaustive"] is False
    assert "# Member secret" not in result.stdout
    assert "Utilise read" in result.stdout
