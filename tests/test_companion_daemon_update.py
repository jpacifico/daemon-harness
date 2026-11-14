"""The guided update of an existing daemon: what the person reads and chooses."""

from contextlib import contextmanager

import pytest
from daemons_companion.desktop.terminal import Quit
from daemons_companion.hosts import update
from daemons_companion.runtime.upgrade import UpgradeRefused


class UI:
    def __init__(self, choice=0):
        self.choice, self.said, self.questions, self.events, self.phases = choice, [], [], [], []
        self.finished = False

    def say(self, text):
        self.said.append(text)

    def choose(self, question, options):
        self.questions.append((question, options))
        return self.choice

    @contextmanager
    def activity(self, message):
        self.said.append(message)
        yield

    def phase(self, name):
        self.phases.append(name)

    def emit(self, event, **values):
        self.events.append((event, values))

    def diagnostic(self, *_args):
        pass


class Upgrade:
    def __init__(self, status="required", review=None, result="committed"):
        self.status, self.review_result, self.result, self.runs = status, review, result, []

    def review(self):
        if isinstance(self.review_result, Exception):
            raise self.review_result
        return {"revision": "r", "status": "review"}

    def run(self, revision, recover=False):
        self.runs.append((revision, recover))
        return {"revision": revision, "status": self.result}


@pytest.fixture
def owned(tmp_path, monkeypatch):
    base = tmp_path / "base"
    profile = base / "Companion/hosts/hermes/installations/x/profile"
    profile.mkdir(parents=True)
    state = {}

    def from_request(request):
        assert request == {"source_profile": str(profile), "base": str(base)}
        return state["upgrade"]

    import daemons_companion.runtime.upgrade as module

    monkeypatch.setattr(module, "from_request", from_request)
    monkeypatch.setattr(
        module, "current_status", lambda upgrade: {"revision": "old", "status": upgrade.status}
    )
    return profile, base, state


def test_current_daemon_is_left_alone(owned):
    profile, base, state = owned
    state["upgrade"] = Upgrade(status="current")
    ui = UI()
    assert update.run(ui, profile, base, "Aster") is False
    assert not ui.questions and not state["upgrade"].runs


def test_update_is_explained_confirmed_and_applied(owned):
    profile, base, state = owned
    state["upgrade"] = Upgrade()
    ui = UI()
    assert update.run(ui, profile, base, "Aster") is True
    assert ui.phases == ["installation"]
    assert ui.questions == [("Update Aster now?", ("Update", "Cancel"))]
    assert "not what it says or remembers" in ui.said[0]
    assert ui.said[-1] == "Aster is up to date."
    assert state["upgrade"].runs == [("r", False)]


def test_declining_changes_nothing(owned):
    profile, base, state = owned
    state["upgrade"] = Upgrade()
    with pytest.raises(Quit):
        update.run(UI(choice=1), profile, base, "Aster")
    assert not state["upgrade"].runs


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("protected-runtime", "install a new daemon from that backup"),
        ("attached-hosts", "cannot update both hosts together"),
        ("openclaw-profile", "OpenClaw settings changed"),
        ("hermes-profile", "Hermes Agent settings changed"),
        ("something-new", "could not be checked"),
    ],
)
def test_each_refusal_is_explained_and_nothing_runs(owned, code, fragment):
    profile, base, state = owned
    state["upgrade"] = Upgrade(review=UpgradeRefused(code, "synthetic"))
    ui = UI()
    with pytest.raises(update.UpdateStopped) as stopped:
        update.run(ui, profile, base, "Aster")
    assert stopped.value.code == code
    assert fragment in ui.said[-1]
    assert not state["upgrade"].runs


def test_a_rolled_back_update_says_the_daemon_is_restored(owned):
    profile, base, state = owned
    state["upgrade"] = Upgrade(result="rolled_back")
    ui = UI()
    with pytest.raises(update.UpdateStopped):
        update.run(ui, profile, base, "Aster")
    assert "restored to its previous version" in ui.said[-1]


def test_an_interrupted_update_offers_to_restore(owned):
    profile, base, state = owned
    state["upgrade"] = Upgrade(status="in_progress")
    ui = UI()
    assert update.run(ui, profile, base, "Aster") is True
    assert ui.questions == [("Restore the previous version?", ("Restore", "Cancel"))]
    assert state["upgrade"].runs == [("old", True)]


def test_add_host_keeps_its_wording():
    texts = update.ADD_HOST
    assert texts.needed == "{name} first needs the update that comes with this app version."
    assert texts.scope == (
        "Its memory, model account and Telegram bot do not change. Its services restart for a few seconds."
    )
    assert (texts.question, texts.confirm) == ("Update your daemon?", "Update and continue")
    assert (texts.restore_question, texts.restore) == ("Restore the previous version?", "Restore and continue")
    assert texts.refusals["protected-runtime"].endswith("To use OpenClaw, create a new daemon with this version.")


def test_wizard_stops_host_addition_on_a_refused_update(owned, monkeypatch):
    from daemons_companion.hosts import wizard

    profile, base, state = owned
    state["upgrade"] = Upgrade(review=UpgradeRefused("attached-hosts", "synthetic"))
    ui = UI()
    with pytest.raises(wizard.HostAdditionStopped) as stopped:
        wizard._update_if_needed(ui, profile, base, "Aster")
    assert stopped.value.code == "attached-hosts"
    assert ui.questions == [("Update your daemon?", ("Update and continue", "Cancel"))]


@pytest.mark.parametrize(
    "upgrade,result",
    [
        (Upgrade(), "installed"),
        (Upgrade(status="current"), "unfinished"),
        (Upgrade(review=UpgradeRefused("protected-runtime", "x")), "unfinished"),
    ],
)
def test_protocol_reports_each_outcome(owned, upgrade, result):
    from daemons_companion.desktop.protocol import serve_daemon_update

    profile, base, state = owned
    state["upgrade"] = upgrade
    ui = UI()
    request = {"source_profile": str(profile), "base": str(base), "name": "Aster"}
    assert serve_daemon_update(ui, request) == 0
    assert ui.events[-1][0] == "finished" and ui.events[-1][1]["result"] == result
    # Every outcome ends on its own sentence, never the generic setup banner.
    assert ui.events[-1][1]["message"]
    assert ui.finished


def test_protocol_refuses_an_unexpected_request():
    from daemons_companion.desktop.protocol import serve_daemon_update

    ui = UI()
    assert serve_daemon_update(ui, {"source_profile": "/x", "base": "/y"}) == 1
    assert ui.events[-1][1]["result"] == "error"


def _catalog_patterns():
    import json
    import re
    from pathlib import Path

    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "apps/companion/desktop/locales/messages.json").read_text()
    )
    return [
        re.compile("^" + re.sub(r"\\\{[a-zA-Z_]+\\\}", ".+", re.escape(entry["en"])) + "$", re.S)
        for entry in catalog.values()
    ]


def test_every_text_of_the_update_is_translated():
    patterns = _catalog_patterns()
    texts = []
    for t in (update.STANDALONE, update.ADD_HOST):
        texts += [t.needed, t.scope, t.question, t.confirm, t.restore_question, t.restore, *t.refusals.values()]
    texts += [update.INTERRUPTED, update.UPDATING, update.ROLLED_BACK, update.UP_TO_DATE]
    shown = [text.format(name="Aster") for text in texts]
    shown += [
        "Aster already runs the version of this app.",
        "The update is finished. Aster's services restarted and were checked.",
        "Updating your daemon.",
        "Your daemon keeps working as it was.",
        "The update was not started. Your daemon keeps working as it was.",
        "The update could not be completed (reference: ValueError). Open the daemon's page again: "
        "an interrupted update is restored from there. Export the diagnostic to report it.",
    ]
    missing = [text for text in shown if not any(p.match(text) for p in patterns)]
    assert not missing
