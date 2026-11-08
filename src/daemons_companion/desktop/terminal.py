"""Shared terminal setup: verified private host acquisition and daemon plan review.

The existing backend remains the only planner. Model authentication and daemon
activation are not enabled by the host acquisition action.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import select
import shutil
import sys
import textwrap
import threading
import time
import unicodedata
import webbrowser

from daemons_companion.desktop.app import (
    CompanionAppRequestError,
    REQUEST_SCHEMA,
    handle_request,
)
from daemons_companion.setup.plan import ProviderKind, ProviderSelection
from daemon_core.daemon_identity import validate_name
from daemons_companion.setup.transaction import TransactionError


# Uniform 5×7 videotex cells: a shallow M notch and a continuous N diagonal,
# matching the construction visible in the user's MINITEL reference.
_LETTERS = {
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10001", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
    "S": ("01110", "10000", "10000", "01110", "00001", "00001", "11110"),
}

# Original pixel capitals inspired by Dervaux metro signs, with stencil cuts.
# 64 columns give equal four-column margins in the 72-column banner.
# Fixed artwork: no font file or system font installation is required.
_DERVAUX_BITMAP = (
    "1111111000001111000111111001110000111000111100011000011000111110",
    "1111111100011111100111111101111001111001111110011100011001111110",
    "1100001110111001110110000001111111111011100111011110011011100000",
    "1100000110110000110110000001100110011011000011011110011011000000",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "1100000110110000110110000001100000011011000011011011011011000000",
    "1100000110110000110111111001100000011011000011011011011011100000",
    "1100000110110000110111111001100000011011000011011011011001111000",
    "1100000110111111110110000001100000011011000011011001111000011110",
    "1100000110111111110110000001100000011011000011011001111000000111",
    "1100000110110000110110000001100000011011000011011001111000000011",
    "1100000110110000110110000001100000011011000011011000111000000011",
    "1100000110110000110110000001100000011011000011011000011000000011",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "1111111100110000110111111101100000011001111110011000011011111110",
    "1111111000110000110111111001100000011000111100011000011011111100",
)

# Explicit RGB values keep the reference palette independent of terminal themes.
_CYAN = "\x1b[38;2;103;180;215m"
_BASE = "\x1b[38;2;213;230;233m\x1b[48;2;5;10;13m"
_BANNER = "\x1b[38;2;218;240;245m\x1b[48;2;77;148;188m"
_INVERSE = "\x1b[38;2;5;10;13m\x1b[48;2;103;180;215m"

TELEGRAM_GUIDE = (
    ("Your own Telegram bot", (
        "Keep the bot already connected to your selected host, if you have one.",
        "Otherwise open https://web.telegram.org/ on this computer, sign in and search for the verified @BotFather account. Send /newbot, enter a display name, then choose an available username ending in bot.",
        "The daemon's name can be the display name. The bot username is a separate Telegram identifier.",
        "This preview does not open accounts, create a bot or change your host.",
    )),
    ("Keep the bot token private", (
        "BotFather gives you a token. Treat it like a password. Never paste it into a chat, a public issue or a shell command.",
        "The completed installer must accept it through masked input and connect it through your host's supported configuration.",
        "Do not paste a token into this preview: credential entry is not enabled.",
        "If this bot is already used elsewhere, resolve that conflict explicitly. Do not start a second receiver or remove its webhook blindly.",
    )),
    ("Open the conversation", (
        "Open the verified bot username in Telegram and press Start. You must initiate the conversation with your bot.",
        "The completed installer will show the verified bot link and a QR for that public link, never for the token.",
        "A QR opens the chat. It does not authorize your account or prove the daemon is connected.",
    )),
    ("Allow only your account", (
        "Follow the selected host's native pairing flow. Confirm the matching request or code and the account it belongs to.",
        "Never grant access to everyone or assume that the first sender is the owner.",
        "This preview cannot approve pairing. The host must own channel access and permissions.",
    )),
    ("Say hello to your daemon", (
        "Once connected, send a message in your own words. The answer must come from the daemon in its host, not a canned installer reply.",
        "This first conversation and its memory maintenance use your configured model access and may incur usage or charges.",
        "The completed installer must check message reception, the actual reply and durable after-turn work. A saved token alone is not success.",
        "If a step times out, show what remains pending. Do not send the message again automatically. Ask for your confirmation if delivery receipts are unavailable.",
        "No bot, account, reply or memory work was verified by reading this guide.",
    )),
)


class Back(Exception):
    """Return to the previous screen without changing any installation."""


class Quit(Exception):
    """End an explicitly cancelled review."""


def safe_text(value: object) -> str:
    """Never interpret control sequences, bidi overrides or CLI escape output."""
    return "".join(
        char if char == "\n" or not unicodedata.category(char).startswith("C") else "?"
        for char in str(value)
    )


def logo_lines(*, plain: bool = False, columns: int = 80) -> tuple[str, ...]:
    cells = sum(len(_LETTERS[letter][0]) for letter in "DAEMONS") + 6
    if plain or columns < cells + 6:
        return ("DAEMONS",)
    width = max(20, min(72, columns - 4))
    if width >= len(_DERVAUX_BITMAP[0]) + 4:
        # One blue pixel row above and below the sixteen-row lettering.
        padding = "0" * len(_DERVAUX_BITMAP[0])
        bitmap = [padding, *_DERVAUX_BITMAP, padding]
    else:
        bitmap = ["0".join(_LETTERS[letter][row] for letter in "DAEMONS") for row in range(7)]
        bitmap.append("0" * len(bitmap[0]))
    blocks = {("0", "0"): " ", ("1", "0"): "▀", ("0", "1"): "▄", ("1", "1"): "█"}
    return tuple(
        "".join(blocks[pair] for pair in zip(bitmap[row], bitmap[row + 1])).center(width)
        for row in range(0, len(bitmap), 2)
    )


class Terminal:
    def __init__(self, *, plain: bool = False, animate: bool = True):
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        encoding = sys.stdout.encoding or "ascii"
        try:
            "█▀▄".encode(encoding)
            unicode_ok = True
        except UnicodeError:
            unicode_ok = False
        self.plain = plain or not unicode_ok or os.environ.get("TERM") == "dumb"
        self.styled = self.interactive and not self.plain and "NO_COLOR" not in os.environ
        self.animate = animate and self.interactive and not self.plain
        self._style_active = False

    def say(self, value: object = "") -> None:
        width = max(20, min(72, shutil.get_terminal_size((80, 24)).columns - 4))
        for line in safe_text(value).split("\n"):
            wrapped = textwrap.wrap(line, width=width, replace_whitespace=False) if line else [""]
            for row in wrapped:
                print((_BASE + "  " + row + _BASE) if getattr(self, "_style_active", False) else row)

    @contextmanager
    def activity(self, label: str):
        """Animate a bounded operation without claiming byte/percentage progress.

        Only the display runs in a thread; the operation and its cancellation
        stay on the calling thread. Stop and join before any following prompt.
        Plain/non-interactive output keeps readable stage lines instead of ANSI.
        """
        if not self.interactive or self.plain or not self.styled:
            self.say(label)
            yield self.say
            return
        stopped = threading.Event()
        lock = threading.Lock()
        started = time.monotonic()
        current = label
        frame = 0
        frames = "|/-\\"

        def draw():
            nonlocal frame
            width = max(10, min(72, shutil.get_terminal_size((80, 24)).columns - 4))
            elapsed = int(time.monotonic() - started)
            text = f"{frames[frame % 4]} {elapsed // 60:02}:{elapsed % 60:02}  {current}"
            sys.stdout.write("\r\x1b[2K  " + _CYAN + text[:width] + _BASE)
            sys.stdout.flush()
            frame += 1

        def report(value):
            nonlocal current
            with lock:
                current = safe_text(value).replace("\n", " ")
                draw()

        def refresh():
            while not stopped.wait(0.12):
                with lock:
                    draw()

        sys.stdout.write("\x1b[?25l")
        report(label)
        worker = threading.Thread(target=refresh, name="daemons-progress", daemon=True)
        worker.start()
        try:
            yield report
        finally:
            stopped.set()
            worker.join()
            sys.stdout.write("\r\x1b[2K\x1b[?25h" + _BASE)
            sys.stdout.flush()

    def heading(self, title: str) -> None:
        if not self.styled:
            self.say("\n" + title)
            return
        width = max(20, min(72, shutil.get_terminal_size((80, 24)).columns - 4))
        print("\n  " + _CYAN + "─" * width + _BASE)
        # Keep the original case/text searchable and screen-reader friendly.
        for row in textwrap.wrap(safe_text(title), width=width):
            print("  " + _CYAN + row + _BASE)
        print("  " + _CYAN + "·" * width + _BASE)

    def controls(self) -> None:
        if not self.styled or shutil.get_terminal_size((80, 24)).columns < 64:
            self.say("Up/down or a number, Enter to choose. B: back. Q: quit.")
            return
        print("  " + _CYAN + "↑ ↓ / 1–9  " + _INVERSE + " Choose " + _BASE
              + "  " + _CYAN + "ENTER " + _INVERSE + " Send " + _BASE
              + "  " + _CYAN + "B " + _INVERSE + " Back " + _BASE
              + "  " + _CYAN + "Q " + _INVERSE + " Quit " + _BASE)

    def menu_rows(self, options: tuple[str, ...], selected: int) -> list[str]:
        width = max(20, min(72, shutil.get_terminal_size((80, 24)).columns - 4))
        rows = []
        for index, label in enumerate(options):
            parts = textwrap.wrap(safe_text(label), width=width - 6) or [""]
            for offset, part in enumerate(parts):
                prefix = f" {index + 1:>2}.  " if offset == 0 else "      "
                colour = _INVERSE if index == selected else _BASE
                rows.append("  " + colour + (prefix + part).ljust(width) + _BASE)
        return rows

    @contextmanager
    def session(self):
        if os.name == "nt" or not self.interactive:
            with self.style():
                yield
            return
        import termios

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        try:
            with self.style():
                yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)

    @contextmanager
    def keys(self):
        """Temporarily use character input; always restore the original mode."""
        if os.name == "nt":
            yield
        else:
            import termios
            import tty

            fd = sys.stdin.fileno()
            original = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd, termios.TCSANOW)
                yield
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, original)

    def key(self, timeout: float | None = None) -> str:
        if os.name == "nt":
            import msvcrt

            deadline = None if timeout is None else time.monotonic() + timeout
            while not msvcrt.kbhit():
                if deadline is not None and time.monotonic() >= deadline:
                    return ""
                time.sleep(0.005)
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        else:
            if not select.select([sys.stdin], [], [], timeout)[0]:
                return ""
            char = os.read(sys.stdin.fileno(), 1).decode("ascii", errors="ignore")
            if char == "\x1b":
                sequence = b""
                for _ in range(2):
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        sequence += os.read(sys.stdin.fileno(), 1)
                return {b"[A": "up", b"[B": "down"}.get(sequence, "")
        if char in ("\x03", "\x04", "\x1a"):
            raise Quit
        return "enter" if char in ("\r", "\n") else char.lower()

    @contextmanager
    def style(self):
        """Enable Windows VT output only for this display and restore its mode."""
        restore = None
        enabled = self.styled
        if enabled and os.name == "nt":
            import ctypes
            from ctypes import wintypes

            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.GetStdHandle.argtypes = [wintypes.DWORD]
            api.GetStdHandle.restype = wintypes.HANDLE
            api.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            api.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = api.GetStdHandle(-11 & 0xFFFFFFFF)
            mode = wintypes.DWORD()
            enabled = bool(api.GetConsoleMode(handle, ctypes.byref(mode)))
            if enabled:
                enabled = bool(api.SetConsoleMode(handle, mode.value | 4))
                if enabled:
                    def restore():
                        api.SetConsoleMode(handle, mode.value)
        try:
            if self.styled and not enabled:
                self.styled = False
            self._style_active = enabled
            if enabled:
                sys.stdout.write(_BASE + "\x1b[2J\x1b[H")
            yield
        finally:
            self._style_active = False
            if enabled:
                sys.stdout.write("\x1b[0m")
                sys.stdout.flush()
            if restore:
                restore()

    def open_link(self, url: str) -> None:
        # Only public, fixed onboarding links; never credentials or commands.
        if url != "https://web.telegram.org/":
            raise ValueError("Unsupported setup link.")
        try:
            opened = webbrowser.open(url, new=2)
        except OSError:
            opened = False
        if opened:
            self.say("Opening Telegram Web in your default browser. Follow the instructions above, then return here to continue.")
        else:
            self.say("Could not open the browser. Type https://web.telegram.org/ in your usual computer browser.")

    def secret(self, prompt: str) -> str:
        import getpass
        import warnings
        if not self.interactive:
            raise ValueError("Secret entry requires an interactive terminal.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass(prompt)
            except getpass.GetPassWarning:
                raise ValueError("Hidden input is unavailable; no token was accepted.") from None

    def welcome(self) -> None:
        columns = shutil.get_terminal_size((80, 24)).columns
        narrow = columns < sum(len(_LETTERS[letter][0]) for letter in "DAEMONS") + 12
        lines = logo_lines(plain=self.plain or narrow, columns=columns)
        animated = self.animate and not narrow
        if animated:
            # Terminal can leave the launch command's Return in the input queue.
            # Only input received after the logo starts may skip it.
            if os.name == "nt":
                import msvcrt
                while msvcrt.kbhit():
                    msvcrt.getwch()
            else:
                import termios
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        frame_delay = 2.8 / sum(map(len, lines))
        self.say("")
        margin = 2  # Same left edge as headings, fields and body text.
        if animated:
            with self.keys():
                next_frame = time.monotonic()
                for line in lines:
                    sys.stdout.write(" " * margin + (_BANNER if self.styled else ""))
                    for char in line:
                        sys.stdout.write(char + ("█\b" if animated else ""))
                        sys.stdout.flush()
                        next_frame += frame_delay
                        if animated and self.key(max(0, next_frame - time.monotonic())):
                            sys.stdout.write(" \b")
                            animated = False
                    sys.stdout.write((" \b" if animated else "") + (_BASE if self.styled else "") + "\n")
        else:
            for line in lines:
                sys.stdout.write(" " * margin + (_BANNER if self.styled else "") + line
                                 + (_BASE if self.styled else "") + "\n")
        sys.stdout.flush()

    def choose(self, title: str, options: tuple[str, ...], *, default: int = 0) -> int:
        self.heading(title)
        redraw = self.styled and len(self.menu_rows(options, default)) + 3 < shutil.get_terminal_size((80, 24)).lines
        if not redraw:
            for index, label in enumerate(options):
                self.say(f"  {index + 1}. {label}")
        self.controls()
        selected = default
        if self.plain:
            while True:
                answer = self._line(f"Choice [{selected + 1}]: ").strip()
                if answer.lower() in {"b", "q"}:
                    raise Back if answer.lower() == "b" else Quit
                if not answer:
                    return selected
                if answer.isdigit() and 1 <= int(answer) <= len(options):
                    return int(answer) - 1
                self.say("Choose one of the numbered options.")
        with self.keys():
            drawn = 0
            try:
                while True:
                    if redraw:
                        if drawn:
                            sys.stdout.write(f"\r\x1b[2K\x1b[{drawn}A\r")
                        rows = self.menu_rows(options, selected)
                        sys.stdout.write("\n".join(rows) + "\n")
                        drawn = len(rows)
                    sys.stdout.write(("  " + _CYAN if self.styled else "\r") + f"Selection: {selected + 1} " + (_BASE if self.styled else ""))
                    sys.stdout.flush()
                    key = self.key()
                    if key == "up":
                        selected = (selected - 1) % len(options)
                    elif key == "down":
                        selected = (selected + 1) % len(options)
                    elif key == "enter":
                        return selected
                    elif key in {"b", "q"}:
                        raise Back if key == "b" else Quit
                    elif key.isdigit() and 1 <= int(key) <= len(options):
                        selected = int(key) - 1
            finally:
                print()

    def _line(self, prompt: str) -> str:
        value = input(prompt)
        if value.strip().lower() == ":back":
            raise Back
        if value.strip().lower() == ":quit":
            raise Quit
        return value

    def field(self, label: str, previous: str = "", *, maximum: int = 160) -> str:
        self.heading(label)
        self.say(":back / :quit")
        if self.styled:
            width = max(20, min(72, shutil.get_terminal_size((80, 24)).columns - 4))
            print("  " + _CYAN + "." * width + _BASE)
        while True:
            prompt = f"[{safe_text(previous)}] > " if previous else "> "
            if self.styled:
                prompt = "  " + _CYAN + prompt + _BASE
            value = self._line(prompt).strip() or previous
            if value and len(value) <= maximum and safe_text(value) == value and "\n" not in value:
                return value
            self.say(f"Enter 1 to {maximum} printable characters.")


def telegram_guide(ui: Terminal) -> None:
    page = 0
    while page < len(TELEGRAM_GUIDE):
        title, paragraphs = TELEGRAM_GUIDE[page]
        ui.say(f"\nTelegram guide {page + 1}/{len(TELEGRAM_GUIDE)}: {title}")
        for paragraph in paragraphs:
            ui.say(paragraph + "\n")
        try:
            choice = ui.choose("Guide only - no connection is being made", ("Next", "Return to review"))
        except Back:
            if page == 0:
                return
            page -= 1
            continue
        if choice == 1:
            return
        page += 1


def run_wizard(ui: Terminal, *, backend=handle_request, packaged: bool = False, host_setup=None, supported_only=False) -> int:
    ui.welcome()
    request_backend = backend

    def backend(request):
        label = 'Preparing your installation plan...' if request.get('action') in {'prepare', 'plan'} else 'Checking available installations...'
        with ui.activity(label):
            return request_backend(request)

    if host_setup is None:
        ui.say("REVIEW PREVIEW - installation and connection are not enabled.")
        ui.say("No API key or bot token will be requested. Your hosts and memory stay unchanged.")
    else:
        ui.say("Private Hermes Agent and OpenClaw installations are available on macOS and Windows.")
        ui.say("Choose your host, connect a model, then start your daemon on Telegram.")
    if host_setup is not None and hasattr(host_setup, "resume") and host_setup.resume(ui):
        return 0
    inspection = backend({"schema": REQUEST_SCHEMA, "action": "inspect"})["inspection"]
    hosts = ("hermes", "openclaw")
    labels = tuple(
        (("Hermes Agent (recommended)" if name == "hermes" else "OpenClaw") + " - separate private installation" if supported_only else
         f"{name.title()} - {inspection['host_setup_options'][name]['action'].replace('_', ' ')}")
        for name in hosts
    )
    selected = hosts.index(inspection["recommended_host"]) if inspection["recommended_host"] in hosts else 0
    name, gender, model, url, kind = "", "feminine", "", "", "codex_cli"
    subscription_ready = False
    user_preferences = None
    saved = (host_setup.setup_progress() if supported_only and host_setup is not None
             and hasattr(host_setup, "setup_progress") else None)
    step = 0
    resume_provider = False
    if saved is not None:
        selected = 0
        host_setup.selected_host = "hermes"
        name, gender = saved.get("name", ""), saved.get("gender", "feminine")
        kind, model = saved.get("provider_kind", "codex_cli"), saved.get("model", "")
        if "language" in saved:
            ui.conversation_language = saved["language"]
        step = next((number for number, field in ((1, "name"), (2, "gender"), (3, "language")) if field not in saved), 4)
        resume_provider = "provider_kind" in saved
        ui.say("Hermes Agent is already installed. Continuing daemon setup without downloading the host again.")
        if not saved:
            ui.say("This older setup did not save its form choices. Enter the missing details once; the installed host and saved Telegram connection are retained.")
    resume_model = bool(saved and saved.get("model"))
    from daemons_companion.hosts.change import inherited_identity, pending_change
    change = pending_change()
    if change is not None and saved is None:
        # The daemon keeps its provider and model; the new host checks them again.
        selected = hosts.index(change.target)
        kind, model = change.identity["kind"], change.identity["model"]
        resume_provider, resume_model = True, bool(model)
    def checkpoint(**changes):
        if supported_only and host_setup is not None and hasattr(host_setup, "save_setup_progress"):
            host_setup.save_setup_progress(**changes)
    while True:
        try:
            if hasattr(ui, "phase"):
                ui.phase("host" if step == 0 else "identity" if step <= 3 else "model" if step <= 6 else "telegram" if supported_only else "review")
            if step == 0:
                if change is None:
                    selected = ui.choose("1. Choose your daemon's host", labels, default=selected)
                if host_setup is not None:
                    host_setup.selected_host = hosts[selected]
                if supported_only and host_setup is not None and hosts[selected] == "openclaw":
                    from daemons_companion.openclaw.first_install import run as openclaw_setup
                    return openclaw_setup(host_setup, ui)
                setup = backend({"schema": REQUEST_SCHEMA, "action": "host_setup", "host": hosts[selected]})["host_setup"]
                if host_setup is not None and hosts[selected] == "hermes" and host_setup.context.target_os in {"macos", "windows"} and setup["action"] == "install_qualified":
                    ui.say("Hermes is absent. A verified private installation is available below.")
                elif not supported_only:
                    ui.say(setup["summary"])
                if host_setup is not None and host_setup.offer(ui, hosts[selected], setup):
                    inspection = backend({"schema": REQUEST_SCHEMA, "action": "inspect"})["inspection"]
                step = 1
            elif step == 1:
                inherited = inherited_identity()
                if inherited is not None:
                    name, gender, language, message = inherited
                    checkpoint(name=name, gender=gender)
                    if language:
                        ui.conversation_language = language
                        checkpoint(language=language)
                    ui.say(message)
                    step = 4 if language else 3
                    continue
                name = ask_daemon_name(ui, name)
                checkpoint(name=name)
                step = 2
            elif step == 2:
                gender = ("feminine", "masculine")[ui.choose(
                    "How should your daemon refer to itself?",
                    ("She / her", "He / him"), default=int(gender == "masculine"),
                )]
                checkpoint(gender=gender)
                step = 3
            elif step == 3:
                if hasattr(ui, "first_meeting_preferences"):
                    ui.first_meeting_preferences()
                    checkpoint(language=getattr(ui, "conversation_language", "en"))
                elif hasattr(ui, "user_preferences"):
                    user_preferences = ui.user_preferences(user_preferences)
                step = 4
            elif step == 4:
                if supported_only:
                    from daemons_companion.setup.model_access import options
                    kinds, provider_labels = options()
                else:
                    kinds = ("codex_cli", "openai_api", "openai_compatible_api", "anthropic_api")
                    provider_labels = ("ChatGPT subscription — sign in with your account", "OpenAI API key",
                                       "OpenAI-compatible provider API key", "Anthropic API key")
                if not resume_provider:
                    kind = kinds[ui.choose("3. Model access to prepare", provider_labels, default=kinds.index(kind))]
                resume_provider = False
                checkpoint(provider_kind=kind)
                subscription_ready = False
                ui.say("Subscription limits or API charges apply to the configured access.")
                if host_setup is not None:
                    connected = host_setup.connect_subscription(ui, hosts[selected], kind)
                    if connected is False:
                        ui.say("Model access is not connected. Complete the connection before continuing setup.")
                        continue
                    subscription_ready = connected is True
                    inspection = backend({"schema": REQUEST_SCHEMA, "action": "inspect"})["inspection"]
                if not subscription_ready:
                    if supported_only:
                        ui.say("Complete your private Hermes installation and model connection before continuing.")
                        step = 0
                        continue
                    ui.say("Preview only: this selection does not connect a model or enable Telegram installation.")
                step = 5
            elif step == 5:
                if subscription_ready and host_setup is not None:
                    model = host_setup.choose_model(ui, model, **({"resume": True} if resume_model else {}))
                    resume_model = False
                    checkpoint(model=model)
                else:
                    model = ui.field("Model identifier (advanced preview only)", model)
                step = 6 if kind == "openai_compatible_api" else 7
            elif step == 6:
                url = ui.field("Provider HTTPS base URL (no credentials)", url, maximum=512)
                ProviderSelection(ProviderKind(kind), model, url, True).validated()
                step = 7
            else:
                response = None
                pronouns = "she/her" if gender == "feminine" else "he/him"
                host_label = "Hermes Agent" if hosts[selected] == "hermes" else "OpenClaw"
                if not supported_only:
                    ui.say(f"\n4. Review: {name} / {pronouns} / {host_label} / {model}")
                if hosts[selected] not in inspection["usable_hosts"]:
                    ui.say("BLOCKED: the selected host is absent or unrecognized. No executable installation plan can be prepared.")
                elif kind == "codex_cli" and not inspection["codex_cli_available"]:
                    ui.say("BLOCKED: ChatGPT access is not ready. Return to model access to connect your account." if host_setup is not None else "BLOCKED: Codex CLI was not found. This preview cannot install it or sign you in.")
                else:
                    request = {
                        "schema": REQUEST_SCHEMA, "action": "prepare" if packaged else "plan",
                        "host": hosts[selected], "daemon_name": name, "daemon_gender": gender,
                        "provider": {"kind": kind, "model": model,
                                     "base_url": url if kind == "openai_compatible_api" else "",
                                     "credential_available": kind != "codex_cli"},
                    }
                    if user_preferences is not None:
                        request["user_preferences"] = user_preferences.to_dict()
                    response = backend(request)
                    if not subscription_ready:
                        ui.say("Credential availability is a planning assumption, not a checked connection.")
                    if not supported_only:
                        ui.say(response.get("review_text", "Public outline only; no executable confirmation outside a verified package."))
                        for item in response["plan"]["steps"]:
                            ui.say("- " + item["description"])
                        if ui.choose("Review details", ("Continue", "Show exact plan")) == 1:
                            ui.say(json.dumps(response, indent=2, ensure_ascii=False))
                if response is not None and host_setup is not None and subscription_ready and hosts[selected] == "hermes" and kind in {"codex_cli", "mistral_api", "openai_api", "anthropic_api", "xai_api"}:
                    if supported_only or ui.choose("Continue installation", ("Install daemon and connect Telegram", "Keep reviewing")) == 0:
                        try:
                            if host_setup.complete(ui, request, response):
                                return 0
                            if supported_only and host_setup.daemon_installed:
                                return 0
                        except Back:
                            # Telegram owns its substeps. Returning from its first
                            # screen returns to model selection in the desktop app.
                            if supported_only:
                                step = 5
                            continue
                choice = ui.choose("Next", ("Read the Telegram walkthrough", "Change my choices", "Finish preview"))
                if choice == 0:
                    telegram_guide(ui)
                elif choice == 1:
                    step = 0
                else:
                    if host_setup is not None and getattr(host_setup, "daemon_installed", False):
                        ui.say("Private daemon services are installed. The first Telegram conversation still needs confirmation.")
                    elif host_setup is not None and host_setup.subscription_connected:
                        ui.say("Private Hermes and ChatGPT sign-ins are ready. Daemon setup is unfinished; no model call or Telegram connection verified.")
                    elif host_setup is not None and host_setup.changed:
                        ui.say("Hermes host installed. Daemon setup is unfinished; no model or Telegram connection verified.")
                    else:
                        ui.say("Preview finished. Nothing installed; no model or Telegram connection verified.")
                    return 0
        except Back:
            resume_provider = resume_model = False
            step = max(0, step - 1)
            if step == 3 and not (hasattr(ui, "first_meeting_preferences") or hasattr(ui, "user_preferences")):
                step = 2
            if step == 6 and kind != "openai_compatible_api":
                step = 5
        except TransactionError as error:
            ui.say("Installation stopped: " + safe_text(error))
            if change is not None and change.state != "prepared":
                return stop_change(ui, change)
            if ui.choose("Installation stopped — no automatic retry", (
                "Close assistant", "Return to choices",
            )) == 0:
                return 1
            step = 0
        except (CompanionAppRequestError, ValueError, RuntimeError) as error:
            from daemons_companion.hosts.change import ChangeError, message
            if isinstance(error, ChangeError) or (change is not None and change.state != "prepared"):
                # After the switch, the previous host is restarted rather than retried.
                ui.say(message(error))
                return stop_change(ui, change) if change is not None else 1
            from daemons_companion.runtime.acquisition import HostDownloadUnavailable
            download = isinstance(error, HostDownloadUnavailable)
            # A download outage is not the user's choice to review: say what
            # happened and offer to retry the same installation.
            ui.say(safe_text(error) if download else "Please review: " + safe_text(error))
            if step >= 7:
                try:
                    choice = ui.choose("Setup did not finish", (
                        "Retry" if download else "Return to review", "Change model access", "Quit"))
                except Back:
                    choice = 0
                if choice == 2:
                    raise Quit
                step = 7 if choice == 0 else 4


def stop_change(ui, change):
    """A host change that cannot finish leaves the daemon on its previous host."""
    change.abandon()
    return 1


def ask_daemon_name(ui, previous=""):
    """Ask until the name is valid, keeping what was typed and saying why."""
    name = previous
    while True:
        name = ui.field("2. What is your daemon's name?", name, maximum=80)
        try:
            return validate_name(name)
        except ValueError as error:
            ui.say(str(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAEMONS terminal setup (private Hermes installation on macOS and Windows).")
    parser.add_argument("--plain", action="store_true", help="Simple text and numbered input.")
    parser.add_argument("--no-animation", action="store_true")
    parser.add_argument("--logo", action="store_true", help="Show only the welcome; do not inspect hosts.")
    args = parser.parse_args(argv)
    ui = Terminal(plain=args.plain, animate=not args.no_animation)
    host_setup = None
    try:
        if args.logo:
            with ui.session():
                ui.welcome()
            return 0
        if not ui.interactive:
            ui.say("Open DAEMONS in an interactive terminal. Piped input is not installation consent.")
            return 2
        packaged = bool(os.environ.get("DAEMON_HARNESS_COMPANION_PACKAGE_ROOT"))
        with ui.session():
            if packaged:
                from daemons_companion.desktop.host_terminal import PackagedHostSetup
                with ui.activity("Checking installer files..."):
                    host_setup = PackagedHostSetup.from_environment(os.environ)
            if host_setup is not None:
                return run_wizard(ui, backend=host_setup.backend, packaged=True, host_setup=host_setup)
            return run_wizard(ui, packaged=False)
    except (Quit, KeyboardInterrupt, EOFError):
        if host_setup is not None:
            ui.say("\nSetup cancelled. Any completed private host installation is retained; daemon connection is unfinished.")
        else:
            ui.say("\nReview cancelled. No installation changes were made.")
        return 130
    except (OSError, RuntimeError, ValueError):
        if host_setup is not None:
            ui.say("Setup could not complete. Keep the private installation journal for recovery; daemon connection is unfinished.")
        else:
            ui.say("The review could not complete. No installation was started. Check the verified package and try again.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
