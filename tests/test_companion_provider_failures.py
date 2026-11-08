"""Provider failures name their provider, so the app can say which service failed."""

import inspect
import json
from pathlib import Path

import pytest

from daemon_core import api_providers as core
from daemon_core.api_providers import ApiConnectionError
from daemons_companion.setup import provider_api

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "apps/companion/desktop/locales/messages.json").read_text())


def test_every_named_message_is_one_the_core_raises():
    # A reworded core message would silently lose its provider name.
    source = inspect.getsource(core)
    for message in provider_api.NAMED:
        assert repr(message) in source, message


@pytest.mark.parametrize("kind", sorted(core.API_PROVIDERS))
def test_every_named_message_is_translated(kind):
    name = core.API_PROVIDERS[kind].label
    english = {row["en"].replace("{service}", name) for key, row in CATALOG.items() if key.startswith("provider.named.")}
    for template in provider_api.NAMED.values():
        assert template.format(provider=name) in english


@pytest.mark.parametrize("call, args", [("model_catalog", ("anthropic_api", "key")),
                                        ("verify_model", ("anthropic_api", "key", "claude-opus-5-5"))])
def test_a_provider_outage_names_the_provider_and_keeps_other_messages(monkeypatch, call, args):
    def unavailable(*_args, **_options):
        raise ApiConnectionError("The provider is unavailable. Try again later.")

    monkeypatch.setattr(core, call, unavailable)
    with pytest.raises(ApiConnectionError) as raised:
        getattr(provider_api, call)(*args)
    assert str(raised.value) == "Anthropic is not answering right now. Your key is kept: try again in a few minutes."

    def incomplete(*_args, **_options):
        raise ApiConnectionError("The provider did not return a complete text response. Try another model.")

    monkeypatch.setattr(core, call, incomplete)
    with pytest.raises(ApiConnectionError) as raised:
        getattr(provider_api, call)(*args)
    assert str(raised.value) == "The provider did not return a complete text response. Try another model."


def test_a_successful_check_is_unchanged(monkeypatch):
    monkeypatch.setattr(core, "model_catalog", lambda kind, key, **options: ("model-a",))
    assert provider_api.model_catalog("openai_api", "key") == ("model-a",)
