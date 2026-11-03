"""Process-only settings for immutable companion releases, including older ones."""
from pathlib import Path


def private_diagnostic_setup(state: Path) -> str:
    """Bind the existing diagnostic sink after the owned environment is loaded.

    This is startup configuration of a technical log, not a memory write or a
    replacement implementation. Inline it in the owned job so a previous frozen
    runtime can keep its exact source files. Model behavior remains unchanged.
    """
    from daemons_companion.runtime.bootstrap import _source_io_path
    target = _source_io_path(state / "diagnostics/scene_model_shadow.jsonl")
    return ("from pathlib import Path;from daemon_core import scene_model;"
            f"scene_model.SHADOW_LOG=Path({str(target)!r});")
