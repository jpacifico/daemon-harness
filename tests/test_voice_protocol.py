from daemon_core.voice_protocol import (
    TurnMetadataStreamFilter,
    strip_internal_turn_metadata,
)


def test_strip_internal_turn_metadata_removes_only_exact_protocol_lines():
    leaked = (
        "[heure locale du tour : 2026-07-25T17:20:22]\n"
        "Tu as raison.\n\n"
        "La soirée commence."
    )

    assert strip_internal_turn_metadata(leaked) == (
        "Tu as raison.\n\nLa soirée commence."
    )
    assert strip_internal_turn_metadata(
        "L'expression [heure locale du tour] existe."
    ) == "L'expression [heure locale du tour] existe."
    committed = (
        "[daemon-turn: clarify] Qui vient ?\n"
        "Je choisirais tout de même la première option."
    )
    assert strip_internal_turn_metadata(committed) == "Qui vient ?"
    assert strip_internal_turn_metadata(
        "Le tag [daemon-turn: clarify] est cité dans une phrase."
    ) == "Le tag [daemon-turn: clarify] est cité dans une phrase."
    assert strip_internal_turn_metadata(
        "[daemon-turn clarify avec espacement imprévu\nQui vient ?"
    ) == "Qui vient ?"


def test_terminal_condition_becomes_visible_only_when_no_body_follows():
    assert strip_internal_turn_metadata(
        "[daemon-turn: condition] Si le japonais livre encore, prends des sashimis."
    ) == "Si le japonais livre encore, prends des sashimis."
    assert strip_internal_turn_metadata(
        "[daemon-turn: condition] Si le japonais livre encore, prends des sashimis.\n"
        "Prends plutôt l'assortiment saumon."
    ) == (
        "Si le japonais livre encore, prends des sashimis.\n"
        "Prends plutôt l'assortiment saumon."
    )


def test_same_line_visible_commitment_preserves_the_model_answer():
    expected = "Je comprends. Ce soir, enfin, tu pourras sortir du flux."

    assert strip_internal_turn_metadata(
        f"[daemon-turn: exchange] {expected}"
    ) == expected
    assert strip_internal_turn_metadata(
        f"[daemon-turn: direct] {expected}"
    ) == expected


def test_repeated_turn_commitments_never_become_visible():
    expected = "Le cafe d'abord."
    repeated = (
        "[daemon-turn: exchange]\n"
        "[daemon-turn: exchange]\n"
        f"{expected}"
    )

    assert strip_internal_turn_metadata(repeated) == expected

    protocol_filter = TurnMetadataStreamFilter()
    assert protocol_filter.feed(repeated) == expected
    assert protocol_filter.finish() == ""


def test_turn_metadata_stream_filter_holds_a_split_leading_annotation():
    protocol_filter = TurnMetadataStreamFilter()

    visible = [
        protocol_filter.feed("[heure locale "),
        protocol_filter.feed("du tour : 2026-07-25T17:20:22]\nTu as "),
        protocol_filter.feed("raison."),
        protocol_filter.finish(),
    ]

    assert "".join(visible) == "Tu as raison."


def test_turn_metadata_stream_filter_ignores_transport_blank_before_annotation():
    protocol_filter = TurnMetadataStreamFilter()

    visible = [
        protocol_filter.feed("\n"),
        protocol_filter.feed("[daemon-tu"),
        protocol_filter.feed("rn: direct]\n"),
        protocol_filter.feed("Réponse visible."),
        protocol_filter.finish(),
    ]

    assert "".join(visible) == "Réponse visible."
    assert strip_internal_turn_metadata(
        "\n[daemon-turn: direct]\nRéponse visible."
    ) == "Réponse visible."


def test_turn_metadata_stream_filter_preserves_same_line_visible_commitment():
    protocol_filter = TurnMetadataStreamFilter()

    visible = [
        protocol_filter.feed(
            "[daemon-turn: exchange] Je comprends.\n"
            "Ce soir, tu pourras sortir du flux."
        ),
        protocol_filter.finish(),
    ]

    assert "".join(visible) == (
        "Je comprends.\nCe soir, tu pourras sortir du flux."
    )


def test_condition_waits_past_blank_transport_chunks():
    protocol_filter = TurnMetadataStreamFilter()

    assert protocol_filter.feed(
        "[daemon-turn: condition] Si la source le confirme, je te le dis.\n"
    ) == ""
    assert protocol_filter.feed("\n") == ""
    assert protocol_filter.feed("La source ne le confirme pas.") == (
        "Si la source le confirme, je te le dis.\n"
        "La source ne le confirme pas."
    )


def test_turn_metadata_stream_filter_does_not_delay_normal_text():
    protocol_filter = TurnMetadataStreamFilter()

    assert protocol_filter.feed("Bonsoir") == "Bonsoir"
    assert protocol_filter.feed(" à toi.") == " à toi."
    assert protocol_filter.finish() == ""


def test_turn_metadata_stream_filter_applies_split_private_clarification():
    protocol_filter = TurnMetadataStreamFilter()

    visible = [
        protocol_filter.feed("[daemon-tu"),
        protocol_filter.feed(
            "rn: clarify] Camille vient-elle ?\n"
        ),
        protocol_filter.feed("Je choisis déjà une berline."),
        protocol_filter.finish(),
    ]

    assert "".join(visible) == "Camille vient-elle ?"


def test_turn_metadata_stream_filter_removes_consecutive_private_lines():
    protocol_filter = TurnMetadataStreamFilter()

    visible = protocol_filter.feed(
        "[heure locale du tour : 2026-07-25T17:20:22]\n"
        "[daemon-turn: direct]\n"
        "Réponse directe."
    )

    assert visible == "Réponse directe."


def test_turn_metadata_stream_filter_applies_terminal_clarification():
    protocol_filter = TurnMetadataStreamFilter()

    assert protocol_filter.feed(
        "[daemon-turn: clarify] Bob participe-t-il à la revue ?"
    ) == ""
    assert protocol_filter.finish() == "Bob participe-t-il à la revue ?"


def test_turn_metadata_stream_filter_preserves_terminal_condition():
    protocol_filter = TurnMetadataStreamFilter()

    assert protocol_filter.feed(
        "[daemon-turn: condition] Si le japonais livre encore, prends des sashimis.\n"
    ) == ""
    assert (
        protocol_filter.finish()
        == "Si le japonais livre encore, prends des sashimis."
    )


def test_turn_metadata_stream_filter_prefixes_condition_before_body():
    protocol_filter = TurnMetadataStreamFilter()

    visible = [
        protocol_filter.feed(
            "[daemon-turn: condition] Si le japonais livre encore, prends des sashimis.\n"
        ),
        protocol_filter.feed("Prends plutôt l'assortiment saumon."),
        protocol_filter.finish(),
    ]

    assert "".join(visible) == (
        "Si le japonais livre encore, prends des sashimis.\n"
        "Prends plutôt l'assortiment saumon."
    )
