from daemon_core.runtime_degradation import (
    DEGRADED_HOST_CONTRACT,
    DEGRADED_RESPONSE_NOTICE,
    add_degraded_response_notice,
    strip_degraded_response_notice,
)


def test_degraded_notice_roundtrip_is_idempotent():
    body = "Je peux tout de même vérifier la météo."

    visible = add_degraded_response_notice(body)

    assert visible == f"{DEGRADED_RESPONSE_NOTICE}\n\n{body}"
    assert add_degraded_response_notice(visible) == visible
    assert strip_degraded_response_notice(visible) == body


def test_degraded_notice_is_an_english_system_message():
    assert DEGRADED_RESPONSE_NOTICE == (
        "⚠️ System notice · Personal memory was unavailable for this response."
    )


def test_degraded_notice_does_not_invent_an_empty_reply():
    assert add_degraded_response_notice("") == ""
    assert strip_degraded_response_notice("") == ""


def test_degraded_host_contract_preserves_tools_without_claiming_memory():
    assert 'native tools' in DEGRADED_HOST_CONTRACT
    assert 'Assert no personal fact' in DEGRADED_HOST_CONTRACT
    assert 'personal memory' in DEGRADED_HOST_CONTRACT
