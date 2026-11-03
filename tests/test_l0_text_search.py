import json

import pytest

from daemon_core.memory import forensics
from daemon_core.memory import l0_search as search
from daemon_core.memory.coverage import RecoveryNeed


def journal(root, name, text):
    target = root / "levels/L0_ground_zero/conversations" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


TEXT = """## 2026-04-14T09:00:00 — assistant
À quelle heure Mila fait-elle de la céramique ?

## 2026-04-14T09:01:00 — user
Elle y va chaque lundi de 15h00 à 16h30.

## 2026-04-14T09:02:00 — assistant
D’accord.
"""
QUERY = {"start": "2025-09-01", "end": "2026-08-31", "terms": ["Mila", "céramique"]}


@pytest.mark.parametrize(
    "change",
    [
        {"start": "2025-01-01"},
        {"end": "2024-01-01"},
        {"start": "2026-02-30"},
        {"start": "20260901"},
        {"terms": ["x"]},
        {"terms": ["*"]},
        {"terms": []},
        {"terms": ["a" * 81]},
        {"terms": ["one", "two", "three", "four"]},
        {"terms": [42]},
    ],
)
def test_query_rejects_unbounded_or_invalid_values(change):
    assert search.parse_text_query({**QUERY, **change}) is None


def test_adjacent_context_finds_pronominal_user_quote_without_crossing_scope(tmp_path):
    good = journal(tmp_path, "alex_2026-04-14.md", TEXT)
    foreign = journal(tmp_path, "other_2026-04-14.md", TEXT)
    late = journal(tmp_path, "alex_2026-09-07.md", TEXT)
    result = search.search_l0_text(
        tmp_path,
        "alex",
        search.parse_text_query(QUERY),
        {p.name: p.stat().st_size for p in (good, foreign, late)},
    )
    assert list(result.sources) == [good.name]
    assert "Elle y va chaque lundi de 15h00 à 16h30." in result.sources[good.name]
    assert result.exact_sources[good.name] == TEXT
    assert result.scanned_files == 1
    assert not result.truncated


def test_terms_are_literal_accent_folded_whole_words(tmp_path):
    note = journal(tmp_path, "alex_2026-04-14.md", TEXT.replace("Mila", "Camila"))
    catalog = {note.name: note.stat().st_size}
    query = search.parse_text_query({**QUERY, "terms": ["Mila", "ceramique"]})
    assert not search.search_l0_text(tmp_path, "alex", query, catalog).sources
    note.write_text(TEXT)
    assert search.search_l0_text(tmp_path, "alex", query, catalog).sources


def test_reader_enforces_scan_and_output_budgets(tmp_path, monkeypatch):
    catalog = {}
    for i in range(1, 15):
        p = journal(tmp_path, f"alex_2026-04-{i:02}.md", TEXT)
        catalog[p.name] = p.stat().st_size
    monkeypatch.setattr(search, "MAX_SEARCH_FILES", 3)
    monkeypatch.setattr(search, "MAX_MATCH_FILES", 2)
    result = search.search_l0_text(
        tmp_path, "alex", search.parse_text_query(QUERY), catalog
    )
    assert result.scanned_files == 3
    assert len(result.sources) == 2
    assert result.truncated
    assert sum(map(len, result.sources.values())) <= search.MAX_MATCH_CHARS
    monkeypatch.setattr(search, "MAX_SEARCH_BYTES", 1)
    result = search.search_l0_text(
        tmp_path, "alex", search.parse_text_query(QUERY), catalog
    )
    assert result.scanned_bytes == 0 and not result.sources and result.truncated


def test_reader_rejects_paths_symlinks_and_oversized_sources(tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text(TEXT)
    link = journal(tmp_path, "alex_2026-04-14.md", "")
    link.unlink()
    link.symlink_to(outside)
    assert not search.search_l0_text(
        tmp_path,
        "alex",
        search.parse_text_query(QUERY),
        {link.name: 1, "../outside.md": 1},
    ).sources
    link.unlink()
    link.write_text(TEXT)
    monkeypatch.setattr(search, "MAX_FILE_BYTES", 1)
    result = search.search_l0_text(
        tmp_path, "alex", search.parse_text_query(QUERY), {link.name: 1}
    )
    assert not result.sources and result.truncated


def test_text_plan_is_not_available_to_chronology():
    raw = json.dumps({"should_search": True, "search": QUERY})
    assert not forensics._parse_forensic_plan(raw, set()).should_search
    parsed = forensics._parse_forensic_plan(raw, set(), allow_text_query=True)
    assert parsed.should_search and parsed.text_query.terms == ("Mila", "céramique")
    assert not parsed.files


def test_personal_fallback_uses_same_two_calls_and_verifies_original_user_quote(
    tmp_path, monkeypatch
):
    path = journal(tmp_path, "alex_2026-04-14.md", TEXT)
    monkeypatch.setattr(forensics, "MEMORY_ROOT", tmp_path)

    class Provider:
        def __init__(self):
            self.calls = []

        def complete(self, messages, temperature=0):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return json.dumps({"should_search": True, "search": QUERY})
            assert "À quelle heure Mila" in messages[-1]["content"]
            return json.dumps(
                {
                    "status": "found",
                    "source_file": path.name,
                    "evidence": "Elle y va chaque lundi de 15h00 à 16h30.",
                    "relation_status": "established",
                    "conclusion": "Lundi, 15h00–16h30.",
                    "confidence": 1,
                }
            )

    provider = Provider()
    result = forensics.attempt_l0_forensic_recovery(
        "alex",
        "Ancien horaire ?",
        "",
        [RecoveryNeed(description="Ancien horaire de Mila")],
        provider,
    )
    assert result.status == "found" and len(provider.calls) == 2
    assert result.search_stats["scanned_files"] == 1
    assert result.source_file == path.name


def test_assistant_claim_cannot_be_returned_as_personal_evidence(tmp_path, monkeypatch):
    text = TEXT.replace("Elle y va chaque lundi de 15h00 à 16h30.", "Je ne sais plus.")
    text = text.replace("D’accord.", "Mila y allait à 15h00, je suppose.")
    path = journal(tmp_path, "alex_2026-04-14.md", text)
    monkeypatch.setattr(forensics, "MEMORY_ROOT", tmp_path)

    class Provider:
        def complete(self, messages, temperature=0):
            if "plan an exceptional" in messages[0]["content"]:
                return json.dumps({"should_search": True, "search": QUERY})
            return json.dumps(
                {
                    "status": "found",
                    "source_file": path.name,
                    "evidence": "Mila y allait à 15h00, je suppose.",
                    "relation_status": "established",
                    "conclusion": "15h00",
                    "confidence": 1,
                }
            )

    result = forensics.attempt_l0_forensic_recovery(
        "alex",
        "Ancien horaire ?",
        "",
        [RecoveryNeed(description="Ancien horaire de Mila")],
        Provider(),
    )
    assert result.status == "invalid_evidence" and result.evidence == ""


def test_no_match_stays_abstention_not_negative_fact(tmp_path, monkeypatch):
    journal(tmp_path, "alex_2026-04-14.md", TEXT)
    monkeypatch.setattr(forensics, "MEMORY_ROOT", tmp_path)

    class Provider:
        calls = 0

        def complete(self, messages, temperature=0):
            self.calls += 1
            return json.dumps(
                {"should_search": True, "search": {**QUERY, "terms": ["basketball"]}}
            )

    provider = Provider()
    result = forensics.attempt_l0_forensic_recovery(
        "alex",
        "Ancien horaire ?",
        "",
        [RecoveryNeed(description="Ancien horaire")],
        provider,
    )
    assert provider.calls == 1 and result.status == "abstained"
    payload = forensics.personal_fact_tool_payload(result)
    assert not payload["evidence"] and payload["relation_status"] == "not_established"


def test_overlapping_topic_cue_keeps_the_later_pronominal_answer(tmp_path):
    text = """## 2026-04-14T09:00:00 — user
Ma fille Mila fait de la céramique depuis septembre 2025.

## 2026-04-14T09:01:00 — assistant
Quel est son créneau ?

## 2026-04-14T09:02:00 — user
Elle y va chaque lundi de 15h00 à 16h30.

## 2026-04-14T09:03:00 — assistant
D’accord.
"""
    path = journal(tmp_path, "alex_2026-04-14.md", text)
    result = search.search_l0_text(
        tmp_path,
        "alex",
        search.parse_text_query(QUERY),
        {path.name: path.stat().st_size},
    )
    selected = result.sources[path.name]
    assert "Quel est son créneau ?" in selected
    assert "Elle y va chaque lundi de 15h00 à 16h30." in selected
    assert selected.count("09:02:00 — user") == 1
