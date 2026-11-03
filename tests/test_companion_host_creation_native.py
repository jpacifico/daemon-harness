"""Opt-in pinned OpenClaw config validation, without network or services.

Set DAEMONS_TEST_OPENCLAW_INSTALLATION to a disposable acquired host root.
Only a new temporary profile is written. All credentials are synthetic.
"""

import json
import os
from pathlib import Path
import subprocess
import pytest
from daemons_companion.hosts.creation import native_config
from daemons_companion.runtime.bootstrap import (
    clean_environment,
    private_home_environment,
)
from daemons_companion.openclaw.host_install import NODE_VERSION


@pytest.mark.parametrize(
    "kind,model",
    [
        ("mistral_api", "mistral-large-4"),
        ("openai_api", "gpt-6-astra"),
        ("codex_cli", "gpt-6-astra"),
        ("anthropic_api", "claude-opus-5-5"),
    ],
)
def test_generated_config_is_accepted_by_pinned_native_openclaw(tmp_path, kind, model):
    installation = os.environ.get("DAEMONS_TEST_OPENCLAW_INSTALLATION")
    if not installation:
        pytest.skip("Requires disposable pinned native OpenClaw installation")
    root = Path(installation)
    node = root / "node" / ("node-v" + NODE_VERSION + "-darwin-arm64") / "bin/node"
    entry = root / "host/node_modules/openclaw/openclaw.mjs"
    profile = tmp_path / "profile"
    profile.mkdir()
    config = profile / "openclaw.json"
    config.write_text(json.dumps(native_config(kind, model, "777001", 18901)))
    env = clean_environment(tmp_path) | private_home_environment(tmp_path / "home")
    env.update(
        OPENCLAW_STATE_DIR=str(profile),
        OPENCLAW_CONFIG_PATH=str(config),
        MISTRAL_API_KEY="synthetic-private-key",
        OPENAI_API_KEY="synthetic-private-key",
        ANTHROPIC_API_KEY="synthetic-private-key",
        TELEGRAM_BOT_TOKEN="987654:" + "b" * 35,
        DAEMONS_GATEWAY_TOKEN="synthetic-private-gateway-token",
    )
    result = subprocess.run(
        [str(node), str(entry), "config", "validate", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"] is True
