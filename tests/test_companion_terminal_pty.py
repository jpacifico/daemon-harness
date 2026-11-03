"""Real terminal transport checks with synthetic host probes, never a profile."""
from __future__ import annotations

import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX PTY transport; Windows console needs its own live test")
ROOT = Path(__file__).resolve().parents[1]


class Console:
    def __init__(self, code, *, command=None, extra_environment=None, control_terminal=False):
        import fcntl
        import pty
        import termios

        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        self.output = b""
        self.pending = b""
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        environment.update(PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", TERM="xterm-256color")
        environment.pop("NO_COLOR", None)
        environment.update(extra_environment or {})
        self.process = subprocess.Popen(
            command or [sys.executable, "-c", code], stdin=self.slave, stdout=self.slave, stderr=self.slave,
            env=environment, start_new_session=True,
            preexec_fn=(lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0)) if control_terminal else None,
        )

    def until(self, expected, timeout=5):
        deadline = time.monotonic() + timeout
        while expected not in self.pending and time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.05)[0]:
                chunk = os.read(self.master, 65536)
                self.output += chunk
                self.pending += chunk
        assert expected in self.pending, self.output.decode(errors="replace")
        self.pending = self.pending.split(expected, 1)[1]

    def send(self, text):
        os.write(self.master, text)

    def finish(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.05)[0]:
                self.output += os.read(self.master, 65536)
        return self.process.wait(timeout=0.1)

    def assert_mode_restored(self):
        import termios

        actual = termios.tcgetattr(self.slave)
        expected = list(self.original)
        # PENDIN is transient kernel input bookkeeping, not a terminal setting.
        actual[3] &= ~getattr(termios, "PENDIN", 0)
        expected[3] &= ~getattr(termios, "PENDIN", 0)
        assert actual == expected

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        os.close(self.master)
        os.close(self.slave)


def test_animated_welcome_can_be_skipped_and_restores_input_mode():
    console = Console("from daemons_companion.desktop.terminal import main; raise SystemExit(main(['--logo']))")
    try:
        console.until("█\b".encode())
        start = time.monotonic()
        console.send(b" ")
        console.until(b"\x1b[0m")
        assert console.finish() == 0
        assert time.monotonic() - start < 2
        console.assert_mode_restored()
        assert b"Press any key" not in console.output
    finally:
        console.close()


def test_interrupted_welcome_restores_input_mode():
    console = Console("from daemons_companion.desktop.terminal import main; raise SystemExit(main(['--logo']))")
    try:
        console.until("█\b".encode())
        # An explicit EOF keystroke exercises the same finally path without
        # sending a signal to the test runner's process group.
        console.send(b"\x04")
        console.until(b"Review cancelled")
        assert console.finish() == 130
        console.assert_mode_restored()
    finally:
        console.close()


def test_welcome_exits_with_real_foreground_terminal_ownership():
    console = Console(
        "from daemons_companion.desktop.terminal import main; raise SystemExit(main(['--logo', '--no-animation']))",
        control_terminal=True,
    )
    try:
        console.until(b"\x1b[0m")
        assert console.finish() == 0
    finally:
        console.close()


def test_real_arrow_navigation_and_fields_finish_synthetic_review():
    code = """
from functools import partial
from daemons_companion.desktop.terminal import Terminal, run_wizard
from daemons_companion.desktop.app import handle_request
from daemons_companion.setup.plan import HostProbe, HostKind
probes = (HostProbe(HostKind.HERMES, True, True, '/synthetic/hermes', '/synthetic/profile', '0.21.5', 'verified'), HostProbe(HostKind.OPENCLAW, False, False))
backend = partial(handle_request, probes=probes, finder=lambda _: None, target_os='macos')
ui = Terminal(animate=False)
with ui.session():
    result = run_wizard(ui, backend=backend)
raise SystemExit(result)
"""
    console = Console(code)
    try:
        console.until(b"Selection: 1")
        console.send(b"\r")
        console.until(b"> ")
        console.send(b"Avery\r")
        console.until(b"Selection: 1")
        console.send(b"\x1b[B\r")  # masculine
        console.until(b"Selection: 1")
        console.send(b"2\r")  # API, but no credential collected in preview
        console.until(b"> ")
        console.send(b"test-model\r")
        console.until(b"Avery / he/him")
        console.until(b"Selection: 1")
        console.send(b"\r")
        console.until(b"Selection: 1")
        console.send(b"3\r")
        console.until(b"Nothing installed")
        assert console.finish() == 0
        console.assert_mode_restored()
    finally:
        console.close()


def test_launch_return_does_not_skip_animation():
    console = Console("import time; time.sleep(0.3); from daemons_companion.desktop.terminal import main; raise SystemExit(main(['--logo']))")
    try:
        console.send(b"\n")
        console.until("█\b".encode())
        started = time.monotonic()
        console.until(b"\x1b[0m", timeout=8)
        assert "▀▀".encode() in console.output.replace("█\b".encode(), b"")
        assert console.finish() == 0
        assert time.monotonic() - started >= 2.0
        console.assert_mode_restored()
    finally:
        console.close()


@pytest.mark.parametrize('failure', ['None', 'RuntimeError()', 'KeyboardInterrupt()'])
def test_activity_keeps_moving_during_work_and_stops_before_next_prompt(failure):
    code = f"""
import threading, time
from daemons_companion.desktop.terminal import Terminal
ui = Terminal(animate=False)
with ui.session():
    try:
        with ui.activity('Downloading and verifying Hermes...') as progress:
            time.sleep(0.45)
            progress('Installing Hermes dependencies...')
            time.sleep(0.15)
            error = {failure}
            if error is not None:
                raise error
    except (RuntimeError, KeyboardInterrupt):
        pass
    assert not any(t.name == 'daemons-progress' for t in threading.enumerate())
    ui.say('NEXT PROMPT')
    time.sleep(0.3)
"""
    console = Console(code)
    try:
        console.until(b'| 00:00  Downloading and verifying Hermes...')
        console.until(b'/ 00:00  Downloading and verifying Hermes...')
        console.until(b'- 00:00  Downloading and verifying Hermes...')
        console.until(b'Installing Hermes dependencies...')
        console.until(b'NEXT PROMPT')
        assert console.finish() == 0
        assert b'\x1b[?25l' in console.output
        assert b'\x1b[?25h' in console.output.split(b'NEXT PROMPT', 1)[0]
        after_prompt = console.output.split(b'NEXT PROMPT', 1)[1]
        assert b'\x1b[2K' not in after_prompt  # No thread redraw after the context.
        console.assert_mode_restored()
    finally:
        console.close()


def test_real_terminal_model_picker_selects_without_typing_identifier():
    code = """
from daemons_companion.desktop.terminal import Terminal
from daemons_companion.setup.model_selection import ModelChoice, select_model
ui = Terminal(animate=False)
with ui.session():
    result = select_model(ui, lambda: tuple(ModelChoice(i, i) for i in ('gpt-5.6-terra', 'other-model', 'gpt-6.1-sol', 'gpt-6-astra')))
    print('RESULT=' + result)
"""
    console = Console(code)
    try:
        console.until(b'Selection: 1')
        assert b'Astra' in console.output and b'Recommended' in console.output
        assert b'Experience may be degraded' not in console.output and b'Terra' not in console.output
        assert b'other-model' not in console.output
        console.send(b'2\r')
        console.until(b'RESULT=gpt-6.1-sol')
        assert console.finish() == 0
        console.assert_mode_restored()
    finally:
        console.close()


def test_real_telegram_web_guide_back_from_id_and_quit_stay_local():
    code = """
from daemons_companion.desktop.terminal import Terminal, Quit
from daemons_companion.setup.telegram import prepare_telegram
ui = Terminal(animate=False)
with ui.session():
    try:
        prepare_telegram(ui, '/synthetic/private')
    except Quit:
        print('TELEGRAM_CANCELLED')
"""
    console = Console(code)
    try:
        console.until(b'Selection: 1')
        console.send(b'2\r')
        console.until(b'Paste the digits from your own Id line')
        console.until(b'> ')
        console.send(b':back\r')
        console.until(b'Connect Telegram Web')
        console.until(b'Selection: 1')
        console.send(b'q')
        console.until(b'TELEGRAM_CANCELLED')
        assert console.finish() == 0
        console.assert_mode_restored()
        assert b'Model identifier' not in console.output
    finally:
        console.close()


def test_actual_telegram_default_then_hidden_retry_without_revisiting_guide():
    code = """
from daemons_companion.desktop.terminal import Terminal
from daemons_companion.setup.telegram import prepare_telegram, verify_telegram_token, TelegramSetupError
ui = Terminal(animate=False)
calls = []
def configure(token):
    calls.append(token)
    if len(calls) == 1:
        raise TelegramSetupError('Telegram rejected this token.')
    assert token == '123456:synthetic-correct-token-abc'
    return {'username': 'synthetic_bot', 'bot_id': 123}
with ui.session():
    assert prepare_telegram(ui, '/synthetic/private') == '12345'
    assert verify_telegram_token(ui, configure)['username'] == 'synthetic_bot'
    assert len(calls) == 2
print('SESSION_CLOSED')
input()
"""
    console = Console(code, control_terminal=True)
    try:
        console.until(b'Selection: 1')
        console.send(b'2\r')
        console.until(b'> ')
        console.send(b'12345\r')
        console.until(b'Create your Telegram bot')
        console.until(b'Selection: 1')
        console.send(b'\r')
        console.until(b'Install and start this private daemon?')
        console.until(b'Selection: 1')
        console.send(b'\r')
        console.until(b'Paste your BotFather token (hidden): ')
        console.send(b'wrong-hidden-token\r')
        console.until(b'Telegram rejected this token.')
        console.until(b'Selection: 1')
        console.send(b'\r')
        console.until(b'Paste your BotFather token (hidden): ')
        console.send(b'123456:synthetic-correct-token-abc\r')
        console.until(b'Token accepted')
        console.until(b'SESSION_CLOSED')
        console.assert_mode_restored()
        console.send(b'\r')
        assert console.finish() == 0
        assert b'wrong-hidden-token' not in console.output
        assert b'123456:synthetic-correct-token-abc' not in console.output
        assert console.output.count(b'Open Telegram Web on this computer') == 1
    finally:
        console.close()
