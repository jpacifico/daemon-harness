"""Native subscription sign-in confined to a companion-owned Hermes installation.

Credentials stay with Codex and Hermes. Never read, copy or rewrite auth files.
Login status is not a model-call or daemon-readiness proof.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess

from daemons_companion.runtime.bootstrap import clean_environment, private_home_environment
from daemons_companion.runtime.host_install import HostInstallLayout, installed_host


@dataclass(frozen=True)
class SubscriptionStatus:
    codex: bool
    hermes: bool


# A read-only native host diagnostic returns only a boolean, never credentials.
_HERMES_STATUS = (
    "from hermes_cli.auth import get_auth_status; "
    "s=get_auth_status('openai-codex'); "
    "print('DAEMONS_AUTH_READY' if s.get('logged_in') is True else 'DAEMONS_AUTH_MISSING')"
)


def native_command(command, environment, *, interactive=False):
    """Let native OAuth own the foreground terminal and browser flow."""
    process = subprocess.Popen(
        command, env=environment, cwd=environment["HOME"],
        stdin=None if interactive else subprocess.DEVNULL,
        stdout=None if interactive else subprocess.PIPE,
        stderr=None if interactive else subprocess.PIPE, text=True,
    )
    try:
        import time
        timeout = 900 if interactive else 30
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException as error:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if isinstance(error, subprocess.TimeoutExpired):
            raise RuntimeError("Native sign-in timed out. Completed sign-ins are retained for retry.") from None
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout or "", stderr or "")


class PrivateSubscriptionSetup:
    def __init__(self, layout: HostInstallLayout, *, codex: Path, runner=native_command,
                 host_reader=installed_host):
        self.layout = layout
        self.codex = codex.resolve()
        if not self.codex.is_absolute() or not self.codex.is_file() or not os.access(self.codex, os.X_OK):
            raise ValueError("A working official Codex CLI is required for subscription setup.")
        self.codex_digest = hashlib.sha256(self.codex.read_bytes()).hexdigest()
        self.runner = runner
        self.host_reader = host_reader

    def _validate(self):
        self.layout.validate()
        host = self.host_reader(self.layout)
        if host is None or host.config_path != str(self.layout.profile):
            raise ValueError("Subscription setup requires the exact active companion-owned profile.")
        if hashlib.sha256(self.codex.read_bytes()).hexdigest() != self.codex_digest:
            raise ValueError("Codex changed during subscription setup. Please reopen the assistant.")
        for path in (self.layout.attempt / "os-home", self.layout.profile):
            if not path.is_dir() or path.is_symlink() or path.resolve() != path:
                raise ValueError("Subscription setup requires direct private directories.")

        for root in (self.layout.attempt / "os-home" / ".codex",
                     self.layout.attempt / "os-home" / ".hermes-codex", self.layout.profile):
            if root.is_symlink() or root.resolve() != root:
                raise ValueError("Private authentication directory must not be a link.")
            for name in ("auth.json", "config.toml", "config.yaml", ".env"):
                if (root / name).is_symlink():
                    raise ValueError("Private authentication files must not be links.")

    def environment(self, *, hermes=False, browser=False):
        environment = clean_environment(self.layout.attempt)
        environment.update(private_home_environment(self.layout.attempt / "os-home"))
        environment["HERMES_HOME"] = str(self.layout.profile)
        # Two independent native sessions prevent refresh-token contention.
        environment["CODEX_HOME"] = str(self.layout.attempt / "os-home" / (".hermes-codex" if hermes else ".codex"))
        environment["PATH"] = str(self.codex.parent) + os.pathsep + os.environ.get("PATH", os.defpath)
        if browser and self.layout.target_os == "windows":
            # The native sign-in launches the user's default browser. Redirecting
            # its Windows profile paths creates a fresh Chrome profile. Preserve
            # those locations only for foreground sign-in; both credential homes
            # remain explicitly confined by HERMES_HOME and CODEX_HOME above.
            for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                if os.environ.get(key):
                    environment[key] = os.environ[key]
        return environment

    def codex_command(self, *args):
        return [str(self.codex), "-c", 'cli_auth_credentials_store="file"',
                "-c", 'forced_login_method="chatgpt"', *args]

    def status(self):
        self._validate()
        codex = self.runner(self.codex_command("login", "status"), self.environment())
        hermes = self.runner([self.layout.launch_python, "-c", _HERMES_STATUS],
                             self.environment(hermes=True))
        if codex.returncode not in (0, 1) or hermes.returncode != 0:
            raise RuntimeError("Native sign-in status could not be checked. Try again from this assistant.")
        return SubscriptionStatus(
            codex.returncode == 0 and "logged in using chatgpt" in (codex.stdout + codex.stderr).lower(),
            hermes.stdout.strip() == "DAEMONS_AUTH_READY",
        )

    def model_choices(self, bridge: Path):
        from daemons_companion.setup.model_selection import parse_catalog
        self._validate()
        # The caller verifies the complete package before supplying this script.
        # Only the private host resolves its own credentials and queries its catalog.
        result = self.runner([self.layout.launch_python, "-B", str(bridge)], self.environment(hermes=True))
        if result.returncode:
            raise RuntimeError("The account model list could not be loaded.")
        try:
            return parse_catalog(result.stdout)
        except (ValueError, RuntimeError):
            raise RuntimeError("The account returned no usable model list.") from None

    def sign_in(self, progress, *, activity=lambda _: nullcontext()):
        with activity("Checking existing ChatGPT connections..."):
            self._validate()
            for name in (".codex", ".hermes-codex"):
                path = self.layout.attempt / "os-home" / name
                if path.is_symlink() or path.resolve() != path:
                    raise ValueError("Private sign-in directory must not be a link.")
                path.mkdir(mode=0o700, exist_ok=True)
            current = self.status()
        if not current.codex:
            progress("ChatGPT connection 1 of 2 — memory work. Sign in in your browser. A second connection will follow for conversations.")
            if self.runner(self.codex_command("login"), self.environment(browser=True), interactive=True).returncode:
                raise RuntimeError("Codex sign-in was cancelled or failed. You can resume it here.")
            # A native UI may exit successfully after the user backs out.
            # Require a real session before opening the next sign-in flow.
            with activity("Verifying ChatGPT sign-in..."):
                current = self.status()
            if not current.codex:
                raise RuntimeError("ChatGPT sign-in was not completed. Setup has not advanced.")
            progress("Memory connection confirmed (1 of 2).")
        if not current.hermes:
            progress("ChatGPT connection 2 of 2 — conversations. Open the link, sign in with the same account and enter the code shown here. This is a separate connection, not a retry of the first one.")
            # Hermes' device-code sign-in: its browser sign-in waits on a
            # one-connection local server that an idle browser connection
            # can block indefinitely, right after the Codex sign-in above.
            command = [str(self.layout.executable), "auth", "add", "openai-codex"]
            if self.runner(command, self.environment(hermes=True, browser=True), interactive=True).returncode:
                raise RuntimeError("Hermes sign-in was cancelled or failed. Completed Codex sign-in is retained.")
        with activity("Verifying both ChatGPT connections..."):
            status = self.status()
        if not status.codex or not status.hermes:
            raise RuntimeError("Both native sign-ins must be confirmed before continuing.")
        return status

