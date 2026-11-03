"""One isolated Claude Opus 5.5 call through Claude Code, on the Claude subscription.

Used by the simulated person and by the judges: no tool, no MCP server, no
project setting, no saved session, an empty working directory, the prompt on
standard input. Protocol: docs/50_continuity_benchmark.md.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

MODEL = "claude-opus-5-5"


def command(claude: Path, mcp_config: Path, *, system: str, effort: str) -> list[str]:
    return [str(claude), "-p", "--model", MODEL, "--effort", effort, "--tools", "",
            "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", str(mcp_config),
            "--system-prompt", system, "--no-session-persistence", "--output-format", "json"]


def call(claude: Path, prompt: str, *, system: str, effort: str, timeout: int) -> tuple[str, dict | None]:
    """Raw standard output, and the parsed envelope when it is one."""
    with tempfile.TemporaryDirectory() as empty:
        mcp = Path(empty) / "mcp.json"
        mcp.write_text('{"mcpServers": {}}')
        work = Path(empty) / "work"
        work.mkdir()
        done = subprocess.run(command(claude, mcp, system=system, effort=effort), input=prompt,
                              text=True, capture_output=True, cwd=work, timeout=timeout)
    try:
        envelope = json.loads(done.stdout)
    except ValueError:
        envelope = None
    return done.stdout, envelope if isinstance(envelope, dict) else None
