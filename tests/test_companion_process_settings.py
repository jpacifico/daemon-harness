import json
import os
import subprocess
import sys

from daemons_companion.runtime.process_settings import private_diagnostic_setup


def test_private_diagnostic_sink_keeps_runtime_tree_unchanged(tmp_path):
    release = tmp_path / 'immutable-release'
    release.mkdir()
    marker = release / 'source-marker.py'
    marker.write_text('# fixed\n')
    state = tmp_path / 'state'
    state.mkdir()
    memory = tmp_path / 'memory'
    memory.mkdir()
    script = (
        "from pathlib import Path;from daemon_core import scene_model;"
        f"scene_model.SHADOW_LOG=Path({str(release / 'exports/scene_model_shadow.jsonl')!r});"
        + private_diagnostic_setup(state)
        + "scene_model._append_shadow_event({'synthetic':True});"
    )
    environment = {**os.environ, 'DAEMON_CORE_MEMORY_ROOT': str(memory), 'PYTHONDONTWRITEBYTECODE': '1'}
    subprocess.run([sys.executable, '-B', '-c', script], env=environment, check=True, timeout=30)
    assert list(release.rglob('*')) == [marker]
    assert marker.read_text() == '# fixed\n'
    assert json.loads((state / 'diagnostics/scene_model_shadow.jsonl').read_text()) == {'synthetic': True}
    assert not list(memory.iterdir())


def test_diagnostic_statement_quotes_path_as_data(tmp_path):
    state = tmp_path / "space ' quote"
    compile(private_diagnostic_setup(state), '<owned-startup>', 'exec')
    assert repr(str(state / 'diagnostics/scene_model_shadow.jsonl')) in private_diagnostic_setup(state)
