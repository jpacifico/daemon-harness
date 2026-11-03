from daemon_core import turn_handoff


def test_turn_handoff_carries_exact_hot_souvenirs(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_handoff, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(turn_handoff.time, "time", lambda: 100.0)

    turn_handoff.write_turn_handoff(
        "u1",
        "Question exacte",
        "trace-1",
        99.5,
    )

    assert turn_handoff.read_turn_handoff(
        "u1",
        "Préfixe\nQuestion exacte",
    ) == ("Question exacte", "trace-1", 99.5)
    assert turn_handoff.write_turn_hot_souvenirs(
        "u1",
        "trace-1",
        [
            "capsules/theme/famille.md",
            "theme/famille",
            "../hors-memoire",
            "theme/travail",
        ],
    ) is True
    assert turn_handoff.read_turn_hot_souvenirs("u1", "trace-1") == (
        "theme/famille",
        "theme/travail",
    )


def test_turn_handoff_rejects_another_or_stale_trace(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(turn_handoff, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(turn_handoff.time, "time", lambda: now[0])
    turn_handoff.write_turn_handoff("u1", "Question", "trace-1", 100.0)

    assert turn_handoff.write_turn_hot_souvenirs(
        "u1",
        "trace-other",
        ["theme/other"],
    ) is False
    assert turn_handoff.read_turn_hot_souvenirs("u1", "trace-other") == ()

    now[0] += turn_handoff.TURN_HANDOFF_MAX_AGE_SECONDS + 1
    assert turn_handoff.write_turn_hot_souvenirs(
        "u1",
        "trace-1",
        ["theme/late"],
    ) is False


def test_hot_souvenir_handoff_survives_a_slow_tool_turn(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(turn_handoff, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(turn_handoff.time, "time", lambda: now[0])
    turn_handoff.write_turn_handoff("u1", "Question", "trace-1", 100.0)
    assert turn_handoff.write_turn_hot_souvenirs(
        "u1",
        "trace-1",
        ["theme/meteo"],
    ) is True

    now[0] += 5 * 60

    assert turn_handoff.read_turn_hot_souvenirs("u1", "trace-1") == (
        "theme/meteo",
    )
