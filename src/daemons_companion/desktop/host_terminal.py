"""Thin terminal connection to the verified host acquisition operation."""
from __future__ import annotations

from dataclasses import replace
import json
import shutil
from pathlib import Path
import uuid

from daemons_companion.desktop.app import handle_request, preparation_context_from_environment
from daemons_companion.setup.plan import detect_hosts
from daemons_companion.runtime.host_install import HostInstallLayout, execute_host_install, host_install_plan, installed_host
from daemons_companion.setup.transaction import public_plan_digest
from daemons_companion.runtime.installation_scope import InstallationScope


class PackagedHostSetup:
    def __init__(self, context, *, execute=execute_host_install):
        self.context = context
        self.execute = execute
        self.changed = False
        self.subscription_connected = False
        self.api_connection = None
        self.daemon_installed = False

    @classmethod
    def from_environment(cls, environment):
        return cls(preparation_context_from_environment(environment))

    def layout(self):
        self.context.validate()
        return HostInstallLayout(
            self.context.companion_home, self.context.uv_executable,
            self.context.uv_sha256, uuid.uuid4().hex, self.context.target_os,
        )

    def backend(self, request):
        probes = detect_hosts()
        context = self.context
        managed = None
        if self.context.target_os in {"macos", "windows"}:
            layout = self.layout()
            managed = installed_host(layout)
            if managed is not None:
                probes = (managed, probes[1])
                if request.get("action") == "prepare" and request.get("host") == "hermes":
                    layout = replace(layout, installation_id=Path(managed.config_path).parent.name)
                    scope = InstallationScope.from_verified_host(layout)
                    context = replace(context, installation_scope=scope)
        codex = self.codex_executable(layout) if managed is not None else None
        def finder(name):
            return str(codex) if name == "codex" and codex else shutil.which(name)
        return handle_request(request, probes=probes, preparation_context=context,
                              finder=finder, target_os=self.context.target_os)

    def setup_progress(self):
        from daemons_companion.setup.progress import load
        scope = getattr(self, "_progress_scope", None)
        if scope is None:
            layout = self.layout()
            host = installed_host(layout)
            if host is None:
                return None
            layout = replace(layout, installation_id=Path(host.config_path).parent.name)
            scope = self._progress_scope = InstallationScope.from_verified_host(layout)
        return load(scope, "hermes")

    def save_setup_progress(self, **changes):
        from daemons_companion.setup.progress import save
        if getattr(self, "_progress_scope", None) is None:
            self.setup_progress()
        scope = getattr(self, "_progress_scope", None)
        if scope is not None:
            save(scope, "hermes", **changes)

    def offer(self, ui, host: str, setup: dict) -> bool:
        preview = getattr(ui, 'allow_preview', True)
        if host != "hermes" or self.context.target_os not in {"macos", "windows"}:
            return False
        # An existing host is left intact; a separate private installation is opt-in.
        if setup["action"] not in {"install_qualified", "reuse_existing", "review_existing"}:
            return False
        with ui.activity("Checking private Hermes setup..."):
            layout = self.layout()
            managed = installed_host(layout)
        if managed is not None:
            return False
        if setup["action"] in {"reuse_existing", "review_existing"}:
            if setup["action"] == "review_existing":
                ui.say("The existing Hermes version could not be approved for the daemon. Install the compatible version separately; its existing profile and services stay unchanged.")
            else:
                ui.say("An existing Hermes installation was found. To keep its profile and services intact, you can create a separate private setup.")
            if ui.choose("Existing Hermes", (
                    "Continue reviewing the existing host" if preview else "Back to host choice",
                    "Set up a separate private Hermes"), **({} if preview else {'default': 1})) == 0:
                if not preview:
                    from daemons_companion.desktop.terminal import Back
                    raise Back()
                return False
        with ui.activity("Preparing the Hermes installation plan..."):
            plan = host_install_plan(layout)
        ui.say("Hermes can now be downloaded and installed in a private location.")
        ui.say("Install Hermes Agent, then connect your model and Telegram.")
        ui.say(f"Destination: {layout.attempt}")
        ui.say("An Internet connection and space for Python and Hermes dependencies are required.")
        while True:
            choice = ui.choose("Install Hermes 0.21.5?", (
                "Not now — continue the preview" if preview else "Back to host choice",
                "Install Hermes", "Show exact installation plan",
            ), **({} if preview else {'default': 1}))
            if choice == 0:
                if not preview:
                    from daemons_companion.desktop.terminal import Back
                    raise Back()
                return False
            if choice == 2:
                ui.say(json.dumps(plan.to_public_dict(), indent=2))
                continue
            break
        # Recheck the package after the human confirmation and before mutation.
        with ui.activity("Verifying installer files..."):
            self.context.validate()
        try:
            with ui.activity("Preparing the private Hermes installation...") as progress:
                result = self.execute(layout, confirmed_digest=public_plan_digest(plan), progress=progress)
        except (KeyboardInterrupt, OSError, RuntimeError, ValueError) as error:
            ui.say("Host setup did not finish. No model, channel or host service was started.")
            ui.say(f"Any incomplete private files and recovery journal are retained in {layout.root}.")
            # Only pre-auth host acquisition details are shown here. Do not
            # expose subprocess output from sign-in or channel operations.
            from daemons_companion.desktop.terminal import safe_text
            from daemons_companion.runtime.bootstrap import HostCommandError
            cause = error
            for _ in range(8):
                if cause.__cause__ is None:
                    break
                cause = cause.__cause__
            ui.say("Installation cause: " + safe_text(str(cause))[:2000])
            if isinstance(cause, HostCommandError) and cause.diagnostic:
                ui.say(safe_text(cause.diagnostic))

            raise
        if result.status != "committed":
            raise RuntimeError("Host installation did not complete its transaction.")
        self.changed = True
        ui.say("Hermes is installed. Model access, the daemon and Telegram still need setup.")
        ui.say("Continue to connect your account and install the daemon.")
        return True

    def connect_subscription(self, ui, host, kind) -> bool | None:
        """True: native sign-ins verified; False: back; None: review-only path."""
        self.subscription_connected = False
        self.api_connection = None
        from daemon_core.api_providers import API_PROVIDERS
        if kind in API_PROVIDERS:
            from daemons_companion.setup.api_setup import connect_api
            return connect_api(self, ui, host, kind)
        if host != "hermes" or kind != "codex_cli" or self.context.target_os not in {"macos", "windows"}:
            return
        from daemons_companion.setup.subscription import PrivateSubscriptionSetup
        with ui.activity('Preparing your ChatGPT connection...'):
            layout = self.layout()
            managed = installed_host(layout)
            if managed is not None:
                layout = replace(layout, installation_id=Path(managed.config_path).parent.name)
        if managed is None:
            ui.say("Subscription sign-in requires a separate private Hermes setup. Return to the host choice to create one.")
            return
        ui.say("Connect your ChatGPT account in the browser. No API key is needed.")
        ui.say("DAEMONS uses the official Codex component for background memory work. If missing, it will be downloaded and verified inside this private installation.")
        ui.say("Hermes and the daemon keep separate native sessions, so two browser sign-ins may be needed. Complete both prompts to continue.")
        ui.say("Existing personal sign-ins are not copied or replaced. Completed private sign-ins are retained if you cancel.")
        with ui.activity('Checking private model access...'):
            self.context.validate()
            codex = self.codex_executable(layout)
        if codex is None:
            from daemons_companion.setup.codex_install import install_private_codex
            with ui.activity("Preparing ChatGPT access...") as progress:
                codex = install_private_codex(layout, progress)
        with ui.activity("Checking the ChatGPT component..."):
            runner = getattr(ui, "subscription_runner", None)
            operation = PrivateSubscriptionSetup(layout, codex=codex, **({"runner": runner} if runner else {}))
        status = operation.sign_in(ui.say, activity=ui.activity)
        if not status.codex or not status.hermes:
            raise RuntimeError("Both native ChatGPT sign-ins must be confirmed before continuing.")
        self.subscription_connected = True
        ui.say("ChatGPT sign-ins are confirmed. Continue to review your model and connect your Telegram bot.")
        return True

    def choose_model(self, ui, previous="", *, resume=False):
        if self.api_connection is not None:
            from daemons_companion.setup.api_setup import choose_api_model
            return choose_api_model(self, ui, previous, resume=resume)
        from daemons_companion.setup.model_selection import select_model
        from daemons_companion.setup.subscription import PrivateSubscriptionSetup

        def load():
            self.context.validate()
            layout = self.layout()
            managed = installed_host(layout)
            if managed is None:
                raise ValueError("A private Hermes installation is required.")
            layout = replace(layout, installation_id=Path(managed.config_path).parent.name)
            codex = self.codex_executable(layout)
            if codex is None:
                raise ValueError("Connect ChatGPT first.")
            bridge = self.context.bundle_root / "integrations/hermes_plugin/companion_models.py"
            return PrivateSubscriptionSetup(layout, codex=codex).model_choices(bridge)

        return select_model(ui, load, previous, resume=resume)

    def codex_executable(self, layout):
        """Return the pinned private Codex, never one found on the computer.

        A personal Codex may be older than the release the recommended model
        requires, so the daemon's memory work would be refused.
        """
        from daemons_companion.setup.codex_install import private_codex
        managed = installed_host(layout)
        if managed is None:
            return None
        return private_codex(replace(layout, installation_id=Path(managed.config_path).parent.name))

    def complete(self, ui, request, response):
        from daemons_companion.hermes.first_install import complete_private_setup
        return complete_private_setup(self, ui, request, response)

    def resume(self, ui):
        from daemons_companion.openclaw.first_install import pointer_path, run
        if pointer_path(self.context).exists():
            run(self, ui)
            return True
        from daemons_companion.hermes.first_install import resume_private_setup
        return resume_private_setup(self, ui)
