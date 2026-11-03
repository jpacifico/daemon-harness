import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

from daemons_companion.hermes.activation import HermesCliBackend, HermesCommandResult
from daemons_companion.hermes.gateway import PrivateHermesControl


def bridge():
    path = Path(__file__).resolve().parents[1] / "integrations/hermes_plugin/companion_control.py"
    spec = importlib.util.spec_from_file_location("isolated_companion_control", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_inventory_delegates_status_without_catalog(monkeypatch, capsys):
    native = ModuleType("hermes_cli.plugins_cmd")
    native._discover_all_plugins = lambda: [(name, "1", "fixture", "user", Path(name), name)
                                          for name in ("daemon_core", "daemon_voice", "unrelated")]
    native._get_enabled_set = lambda: {"daemon_core"}
    native._get_disabled_set = lambda: {"daemon_voice"}
    native._category_active_names = lambda: set()
    calls = []
    def status(name, enabled, disabled, **kw):
        calls.append(name)
        return "disabled" if name in disabled else "enabled" if name in enabled else "not enabled"
    native._plugin_status = status
    parent = ModuleType("hermes_cli")
    parent.plugins_cmd = native
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    bridge().main(["plugins", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == [
        {"name": "daemon_core", "status": "enabled"}, {"name": "daemon_voice", "status": "disabled"}]
    assert calls == ["daemon_core", "daemon_voice"]


@pytest.mark.parametrize("args", [["plugins", "enable", "unrelated"], ["plugins", "install", "daemon_core"], ["gateway", "start"]])
def test_bridge_rejects_unmanaged_operations(args):
    with pytest.raises(ValueError, match="Unsupported"):
        bridge().main(args)


def test_native_disabled_state_is_valid(tmp_path):
    exe = tmp_path / "hermes"
    exe.touch()
    def run(command, environment):
        return HermesCommandResult(0, '[{"name":"daemon_voice","status":"disabled"}]') if command[1] == "plugins" else HermesCommandResult(1)
    state = HermesCliBackend(exe, tmp_path, runner=run).state()
    assert state.plugins == (("daemon_core", False), ("daemon_voice", False))


@pytest.mark.parametrize("target", ["windows", "macos"])
def test_private_control_uses_owned_bridge_and_closed_stdin(tmp_path, monkeypatch, target):
    from daemons_companion.hermes import gateway as module
    release = tmp_path / ("long-root-" * 12) / ("deep-" * 25)
    control = object.__new__(PrivateHermesControl)
    control.scope, control.runtime = object(), object()
    control.host_layout = SimpleNamespace(target_os=target, executable=tmp_path / "hermes",
                                          launch_python="owned-python", attempt=tmp_path)
    control._config_snapshot = None
    monkeypatch.setattr(module, "private_process_environment", lambda *a, **k: {"HERMES_HOME": "private"})
    monkeypatch.setattr(module, "verify_current_release", lambda runtime: release)
    seen = []
    def run(command, **kw):
        seen.append((command, kw))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    control._native((str(control.host_layout.executable), "plugins", "list", "--json"), {})
    command, options = seen[0]
    assert command == ("owned-python", "-B", str(release / "integrations/hermes_plugin/companion_control.py"), "plugins", "list", "--json")
    assert options["stdin"] == module.subprocess.DEVNULL
    assert options["timeout"] == 45
    assert options["env"] == {"HERMES_HOME": "private"}
