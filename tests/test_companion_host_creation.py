"""Private host setup resumes without touching canonical memory or starting jobs."""

from types import SimpleNamespace
import json
import zipfile
from dataclasses import replace
import pytest
import test_companion_openclaw_host_install as acquisition_fixture
from daemons_companion.hosts import creation
from daemons_companion.openclaw.host_install import (
    install,
    plan,
    NODE_VERSION,
    OPENCLAW_VERSION,
)


class Store:
    def __init__(self):
        self.values = {}

    def get(self, ref):
        return self.values.get(ref)

    def put(self, ref, value):
        assert len(value.encode()) <= 2048
        self.values[ref] = value


@pytest.fixture(params=["macos", "windows"])
def setup(tmp_path, request, monkeypatch):
    host = acquisition_fixture.layout.__wrapped__(tmp_path)
    host = replace(host, target_os=request.param)
    from daemons_companion.hosts import attachment_command

    monkeypatch.setattr(
        attachment_command, "native_target", lambda: request.param
    )
    source = tmp_path / "original/profile"
    source.mkdir(parents=True)
    (source / ".env").write_text("TELEGRAM_BOT_TOKEN=123456:" + "a" * 35)
    immutable = tmp_path / "memory.md"
    immutable.write_text("Synthetic memory: unchanged.")
    binding = SimpleNamespace(
        user_id="777001", descriptor=lambda: {"memory": str(immutable), "name": "Aster"}
    )
    store = Store()

    def runner(command, env, **kwargs):
        if command[-1] == "--version":
            return "v" + NODE_VERSION if len(command) == 2 else OPENCLAW_VERSION
        p = host.host / "node_modules/openclaw"
        p.mkdir(parents=True)
        (p / "package.json").write_text(json.dumps({"version": OPENCLAW_VERSION}))
        (p / "openclaw.mjs").write_text("synthetic")
        return ""

    def download(url, out, sha):
        if host.target_os == "macos":
            acquisition_fixture.node_archive(out, host.node_root.name)
        else:
            with zipfile.ZipFile(out, "w") as archive:
                archive.writestr(host.node_root.name + "/node.exe", b"MZ-synthetic")
                archive.writestr(
                    host.node_root.name + "/node_modules/npm/bin/npm-cli.js",
                    b"fake-npm",
                )

    def installer(layout, revision, **kwargs):
        return install(
            layout,
            revision,
            runner=runner,
            downloader=download,
            **kwargs,
        )

    def factory(**overrides):
        return creation.Creation(
            host,
            source,
            binding,
            store=store,
            installer=installer,
            catalog=overrides.get(
                "catalog", lambda kind, key: ("mistral-large-4", "second-model")
            ),
            model_check=overrides.get("model_check", lambda *args: "OK"),
            bot_check=overrides.get("bot_check", lambda token: "SyntheticExample_bot"),
        )

    return host, source, binding, store, factory, immutable


def advance(factory):
    data = factory().start(18901)
    revision = data["revision"]
    data = factory().perform("download", revision, {})
    assert data["phase"] == "key"
    data = factory().perform(
        "key", revision, {"kind": "mistral_api", "key": "synthetic-private-key"}
    )
    assert data["phase"] == "model"
    return revision


def test_resume_every_phase_secret_store_and_no_memory_write(setup):
    host, source, binding, store, factory, memory = setup
    revision = advance(factory)
    assert factory().current()["phase"] == "model"
    factory().perform("model", revision, {"model": "mistral-large-4"})
    assert factory().current()["phase"] == "telegram"
    token = "987654:" + "b" * 35
    result = factory().perform("telegram", revision, {"token": token})
    assert result["phase"] == "ready"
    config = (host.profile / "openclaw.json").read_text()
    public = json.dumps(result) + creation.record_path(host).read_text() + config
    assert token not in public and "synthetic-private-key" not in public
    assert memory.read_text() == "Synthetic memory: unchanged."
    env = creation.credential_environment(host, store=store)
    assert (
        env["TELEGRAM_BOT_TOKEN"] == token
        and env["MISTRAL_API_KEY"] == "synthetic-private-key"
    )
    assert (
        json.loads(config)["models"]["providers"]["daemons-mistral"]["models"][0][
            "compat"
        ]["supportsDeveloperRole"]
        is False
    )
    assert not list(host.attempt.glob("*.plist"))


def test_model_and_token_failures_retain_current_step_and_secret(setup):
    host, source, binding, store, factory, memory = setup
    revision = advance(factory)

    def fail(*args):
        raise ValueError("private failure must not be persisted")

    with pytest.raises(ValueError):
        factory(model_check=fail).perform(
            "model", revision, {"model": "mistral-large-4"}
        )
    assert factory().current()["phase"] == "model"
    assert "private failure" not in creation.record_path(host).read_text()
    factory().perform("model", revision, {"model": "second-model"})
    with pytest.raises(ValueError):
        factory(bot_check=fail).perform(
            "telegram", revision, {"token": "987654:" + "b" * 35}
        )
    assert factory().current()["phase"] == "telegram"
    assert not (host.profile / "openclaw.json").exists()


def test_shared_token_and_unknown_model_refused_before_network(setup):
    _, _, _, _, factory, _ = setup
    revision = advance(factory)

    def unreachable(*args):
        pytest.fail("External verification must not happen")

    with pytest.raises(ValueError):
        factory(model_check=unreachable).perform(
            "model", revision, {"model": "not-listed"}
        )
    factory().perform("model", revision, {"model": "second-model"})
    with pytest.raises(ValueError):
        factory(bot_check=unreachable).perform(
            "telegram", revision, {"token": "123456:" + "a" * 35}
        )


def test_source_change_and_user_configuration_never_overwritten(setup):
    host, source, binding, _, factory, _ = setup
    revision = advance(factory)
    original = binding.descriptor
    binding.descriptor = lambda: {"memory": "different"}
    with pytest.raises(ValueError):
        factory().current()
    binding.descriptor = original
    factory().perform("model", revision, {"model": "second-model"})
    config = host.profile / "openclaw.json"
    config.write_text('{"user-change": true}')
    with pytest.raises(ValueError):
        factory().perform("telegram", revision, {"token": "987654:" + "b" * 35})
    assert config.read_text() == '{"user-change": true}'


def test_partial_acquisition_resumes_only_owned_empty_profile(setup):
    host, _, _, _, factory, _ = setup
    record = factory().start(18901)
    host.attempt.mkdir(parents=True)
    host.profile.mkdir()
    host.receipt.write_text(json.dumps(plan(host) | {"status": "preparing"}))
    (host.attempt / "node").mkdir()
    (host.attempt / "node/partial").write_text("interrupted download")
    assert factory().perform("download", record["revision"], {})["phase"] == "key"
    assert not (host.attempt / "node/partial").exists()


def test_config_publication_recovers_after_crash(setup, monkeypatch):
    host, _, _, _, factory, _ = setup
    revision = advance(factory)
    factory().perform("model", revision, {"model": "second-model"})
    save = creation.save

    def crash_after_config(host, data):
        if data["phase"] == "ready":
            raise KeyboardInterrupt()
        save(host, data)

    monkeypatch.setattr(creation, "save", crash_after_config)
    with pytest.raises(KeyboardInterrupt):
        factory().perform("telegram", revision, {"token": "987654:" + "b" * 35})
    monkeypatch.setattr(creation, "save", save)
    assert factory().current()["phase"] == "ready"
    assert creation.read_record(host)["phase"] == "ready"


def test_interrupted_native_command_is_not_pruned_or_restarted(setup):
    host, _, _, _, factory, _ = setup
    record = factory().start(18901)
    host.attempt.mkdir(parents=True)
    host.profile.mkdir()
    host.receipt.write_text(
        json.dumps(plan(host) | {"status": "preparing", "command_active": True})
    )
    partial = host.attempt / "node"
    partial.mkdir()
    (partial / "owned").write_text("possibly still in use")
    with pytest.raises(ValueError, match="needs recovery"):
        factory().perform("download", record["revision"], {})
    assert (partial / "owned").read_text() == "possibly still in use"


def test_rotated_source_bot_token_is_still_refused(setup):
    _, _, _, _, factory, _ = setup
    revision = advance(factory)
    factory().perform("model", revision, {"model": "second-model"})

    def unreachable(*args):
        pytest.fail("Same bot must be rejected before network")

    with pytest.raises(ValueError, match="separate bot"):
        factory(bot_check=unreachable).perform(
            "telegram", revision, {"token": "123456:" + "z" * 35}
        )


def test_gateway_launch_resolves_only_its_owned_credentials(setup, monkeypatch):
    from daemons_companion.hosts.attachment_entry import host_environment

    host, source, _, store, factory, _ = setup
    revision = advance(factory)
    factory().perform("model", revision, {"model": "second-model"})
    token = "987654:" + "b" * 35
    factory().perform("telegram", revision, {"token": token})
    monkeypatch.setattr(creation, "system_credential_store", lambda: store)
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "a" * 35)
    env = host_environment(host.profile)
    assert env["TELEGRAM_BOT_TOKEN"] == token
    assert env["MISTRAL_API_KEY"] == "synthetic-private-key"
    assert env["OPENCLAW_STATE_DIR"] == str(host.profile)
    assert "HERMES_HOME" not in env
