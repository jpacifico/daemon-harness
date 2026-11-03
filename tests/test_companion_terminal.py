from __future__ import annotations

from functools import partial
from contextlib import contextmanager
import io
import json
from pathlib import Path

import pytest

from daemons_companion.desktop.app import handle_request
from daemons_companion.setup.plan import HostKind, HostProbe
from daemons_companion.desktop.terminal import (
    Back, Quit, TELEGRAM_GUIDE, Terminal, logo_lines, main, run_wizard,
    safe_text, telegram_guide,
)


class FakeUI:
    def __init__(self, *, choices=(), fields=()):
        self.choices = iter(choices)
        self.fields = iter(fields)
        self.output = []

    def welcome(self):
        self.output.append("DAEMONS")

    def say(self, value=""):
        self.output.append(str(value))

    @contextmanager
    def activity(self, label):
        self.say(label)
        yield self.say

    def choose(self, title, options, *, default=0):
        self.output.append(title)
        value = next(self.choices)
        if isinstance(value, Exception):
            raise value
        assert 0 <= value < len(options)
        return value

    def field(self, label, previous="", *, maximum=160):
        self.output.append(label)
        value = next(self.fields)
        if isinstance(value, Exception):
            raise value
        return value


def backend(*, host="hermes", version="0.21.5", codex=True):
    probes = tuple(HostProbe(
        kind, kind.value == host, kind.value == host,
        f"/test/{kind.value}", f"/test/.{kind.value}", version,
        "verified" if kind.value == host else "unavailable",
    ) for kind in HostKind)
    return partial(handle_request, probes=probes,
                   finder=lambda _: "/test/codex" if codex else None,
                   target_os="macos")


def test_logo_scales_to_terminal_and_plain_is_available():
    assert len(logo_lines()) == 9
    assert all(len(line) == 72 for line in logo_lines())
    # Check the occupied envelope, not individual rows of rounded glyphs.
    occupied = [column for row in logo_lines() for column, char in enumerate(row) if char != " "]
    assert min(occupied) == 4
    assert 71 - max(occupied) == 4
    assert len(logo_lines(columns=60)) == 4
    assert all(len(line) == 56 for line in logo_lines(columns=60))
    assert any("█" in line for line in logo_lines())
    assert logo_lines(plain=True)[0] == "DAEMONS"


@pytest.mark.parametrize("value", ["\x1b[2J", "\x9b2J", "\u202ereversed", "\x00", "\rhidden"])
def test_untrusted_terminal_controls_are_neutralized(value):
    cleaned = safe_text(value)
    assert cleaned != value
    assert "\x1b" not in cleaned and "\u202e" not in cleaned


def test_safe_text_retains_normal_multilingual_text():
    assert safe_text("Maël\nAvery & Iris") == "Maël\nAvery & Iris"


@pytest.mark.parametrize("host,version,selection", [("hermes", "0.21.5", 0), ("openclaw", "2026.9.1-beta.1", 1)])
def test_complete_review_uses_existing_backend_without_installation(host, version, selection):
    ui = FakeUI(choices=[selection, 0, 1, 0, 2], fields=["Avery", "test-model"])
    assert run_wizard(ui, backend=backend(host=host, version=version)) == 0
    output = "\n".join(ui.output)
    assert "Nothing installed" in output
    assert "planning assumption" in output
    assert "Public outline only" in output


@pytest.mark.parametrize("host,version", [("absent", ""), ("hermes", "99.0")])
def test_absent_and_unknown_hosts_are_not_silently_approved(host, version):
    ui = FakeUI(choices=[0, 1, 1, 2], fields=["Avery", "test-model"])
    assert run_wizard(ui, backend=backend(host=host, version=version)) == 0
    assert any("BLOCKED:" in text for text in ui.output)
    assert not any("Public outline only" in text for text in ui.output)


def test_codex_missing_remains_a_real_blocker():
    ui = FakeUI(choices=[0, 0, 0, 2], fields=["Avery", "test-model"])
    assert run_wizard(ui, backend=backend(codex=False)) == 0
    assert any("Codex CLI was not found" in text for text in ui.output)


def test_provider_url_validation_can_be_corrected():
    ui = FakeUI(choices=[0, 0, 2, 0, 2],
                fields=["Avery", "test-model", "https://user:private@example.invalid", "https://example.invalid/v1"])
    assert run_wizard(ui, backend=backend()) == 0
    assert any("without credentials" in text for text in ui.output)
    assert all("user:private" not in text for text in ui.output)


def test_back_from_model_access_returns_to_gender():
    ui = FakeUI(choices=[0, 0, Back(), 1, 1, 0, 2], fields=["Avery", "test-model"])
    assert run_wizard(ui, backend=backend()) == 0
    assert any("Avery / he/him" in text for text in ui.output)


def test_back_from_name_returns_to_host():
    ui = FakeUI(choices=[0, 0, 0, 1, 0, 2], fields=[Back(), "Avery", "test-model"])
    assert run_wizard(ui, backend=backend()) == 0


def test_exact_plan_matches_existing_public_plan():
    ui = FakeUI(choices=[0, 0, 1, 1, 2], fields=["Avery", "test-model"])
    run_wizard(ui, backend=backend())
    result = next(json.loads(text) for text in ui.output if text.startswith("{\n"))
    expected = backend()({
        "schema": "daemon.companion-request.v1", "action": "plan", "host": "hermes",
        "daemon_name": "Avery", "daemon_gender": "feminine",
        "provider": {"kind": "openai_api", "model": "test-model", "base_url": "", "credential_available": True},
    })
    assert result == expected


def test_packaged_review_requests_existing_prepare_action():
    real = backend()
    actions = []

    def capture(request):
        actions.append(request["action"])
        if request["action"] == "prepare":
            # The actual context/digest validation is tested in companion_app.
            return real({**request, "action": "plan"})
        return real(request)

    ui = FakeUI(choices=[0, 0, 1, 0, 2], fields=["Avery", "test-model"])
    run_wizard(ui, backend=capture, packaged=True)
    assert actions == ["inspect", "host_setup", "prepare"]


def test_telegram_guide_covers_first_reply_and_memory_without_claiming_connection():
    ui = FakeUI(choices=[0] * len(TELEGRAM_GUIDE))
    telegram_guide(ui)
    text = "\n".join(ui.output)
    for required in ("BotFather", "/newbot", "Start", "pairing", "after-turn", "No bot, account, reply"):
        assert required in text
    assert "no connection is being made" in text


def test_telegram_back_and_return():
    ui = FakeUI(choices=[0, Back(), 1])
    telegram_guide(ui)
    assert sum("1/5" in text for text in ui.output) == 2


def test_no_backend_call_for_logo(monkeypatch, capsys):
    monkeypatch.setattr("daemons_companion.desktop.terminal.handle_request", lambda _: pytest.fail("unexpected inspection"))
    assert main(["--logo", "--plain"]) == 0
    assert "DAEMONS" in capsys.readouterr().out


def test_non_interactive_input_is_not_consent(capsys):
    assert main(["--plain"]) == 2
    assert "Piped input is not installation consent" in " ".join(capsys.readouterr().out.split())


def test_cancellation_has_distinct_exit_code(monkeypatch, capsys):
    from contextlib import nullcontext

    monkeypatch.setattr(Terminal, "__init__", lambda self, **_: setattr(self, "interactive", True))
    monkeypatch.setattr(Terminal, "session", lambda self: nullcontext())
    monkeypatch.setattr("daemons_companion.desktop.terminal.run_wizard", lambda *a, **k: (_ for _ in ()).throw(Quit()))
    assert main([]) == 130
    assert "cancelled" in capsys.readouterr().out


def test_plain_menu_validation_and_back(monkeypatch):
    ui = Terminal(plain=True)
    answers = iter(["abc", "9", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert ui.choose("Pick", ("A", "B")) == 1
    monkeypatch.setattr("builtins.input", lambda _: "b")
    with pytest.raises(Back):
        ui.choose("Pick", ("A", "B"))


def test_fields_allow_short_names_and_reject_controls(monkeypatch):
    ui = Terminal(plain=True)
    values = iter(["\x1b[2J", "b"])
    monkeypatch.setattr("builtins.input", lambda _: next(values))
    assert ui.field("Name") == "b"


def test_no_color_does_not_change_mosaic_shape(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    ui = Terminal()
    assert not ui.styled


def test_launchers_use_terminal_without_mac_gui_fallback():
    root = Path(__file__).resolve().parents[1]
    mac = (root / "apps/companion/macos/main.swift").read_text()
    win = (root / "apps/companion/windows/DaemonHarnessCompanion.ps1").read_text()
    assert "daemons_companion.desktop.terminal" in mac and "daemons_companion.desktop.terminal" in win
    assert "private let application = NSApplication.shared" not in mac
    launcher = (root / "apps/companion/macos/Start-Daemons.command").read_text()
    assert "daemons_companion.desktop.terminal" in launcher
    assert "Contents/MacOS" not in launcher
    assert "build-app.sh" not in (root / "apps/companion/macos/build-terminal.sh").read_text()
    assert "execve(runtime.python.path, &argv, &envp)" in mac
    assert win.index("if ($Terminal)") < win.index("Add-Type -AssemblyName System.Windows.Forms")
    for path in (root / "apps/companion/macos/Start-Daemons.command", root / "apps/companion/windows/Start-Daemons.ps1"):
        source = path.read_text()
        assert "ExecutionPolicy" not in source and "Bypass" not in source
    assert "Start-Daemons.ps1" in (root / "apps/companion/windows/Build-CompanionPackage.ps1").read_text()


def test_unstyled_output_has_no_ansi(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    Terminal(plain=True).welcome()
    assert "\x1b" not in output.getvalue()


def test_failed_host_transaction_stops_without_reoffering_install():
    from types import SimpleNamespace
    from daemons_companion.setup.transaction import TransactionApplyError
    calls = []
    def fail(*args):
        calls.append(1)
        raise TransactionApplyError("Installation failed and rolled back.")
    setup = SimpleNamespace(context=SimpleNamespace(target_os="windows"), offer=fail)
    ui = FakeUI(choices=[0, 0])
    assert run_wizard(ui, backend=backend(), host_setup=setup) == 1
    assert calls == [1]
    assert "Installation stopped — no automatic retry" in ui.output


def test_plain_progress_is_readable_without_control_sequences(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    ui = Terminal(plain=True)
    with ui.activity('Downloading Hermes...') as progress:
        progress('Verifying Hermes...')
    assert output.getvalue().splitlines() == ['Downloading Hermes...', 'Verifying Hermes...']
    assert '\x1b' not in output.getvalue()
