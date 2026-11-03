"""Input integrity and isolation for the private, non-public PARGA bench."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/qualify_parga_embeddings.py"
spec = importlib.util.spec_from_file_location("parga_measurement", SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def fixture(tmp_path):
    root = tmp_path / "inputs"
    relative = "Areas/Testing/A note.md"
    source = root / "snapshot" / relative
    source.parent.mkdir(parents=True)
    source.write_text("# A fictional note\nA factual source.\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {"notes": {relative: digest}}
    cases = {"language": "fr", "repetitions": 2, "cases": [
        {"id": f"C{i:02}", "evidence": [{"path": relative, "sha256": digest}]}
        for i in range(1, 7)]}
    (root / "snapshot-manifest.json").write_text(json.dumps(manifest))
    (root / "cases.json").write_text(json.dumps(cases))
    return root, source


def test_fixed_counterbalance_and_no_repeated_observation(tmp_path):
    root, _ = fixture(tmp_path)
    _, data = bench.inputs(SimpleNamespace(inputs=root))
    plan = bench.schedule(data["cases"])
    assert len(plan) == len(set(plan)) == 24
    for case in data["cases"]:
        first = [arm for name, repeat, arm in plan if name == case["id"] and repeat == 1]
        second = [arm for name, repeat, arm in plan if name == case["id"] and repeat == 2]
        assert first == second[::-1]
        assert set(first) == {"top12", "full"}


def test_corrupted_snapshot_rejected(tmp_path):
    root, source = fixture(tmp_path)
    source.write_text("Changed after preregistration")
    with pytest.raises(ValueError, match="fixed manifest"):
        bench.inputs(SimpleNamespace(inputs=root))


def test_symlink_outside_snapshot_rejected(tmp_path):
    root, source = fixture(tmp_path)
    outside = tmp_path / "private-original.md"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ValueError, match="escaped"):
        bench.inputs(SimpleNamespace(inputs=root))


def test_unrelated_personal_surface_cannot_enter_snapshot(tmp_path):
    root, _ = fixture(tmp_path)
    (root / "snapshot-manifest.json").write_text(json.dumps({"notes": {".daemon/users/real/context.md": "irrelevant"}}))
    with pytest.raises(ValueError, match="five root folders"):
        bench.inputs(SimpleNamespace(inputs=root))


def test_evidence_cannot_drift_from_notes(tmp_path):
    root, _ = fixture(tmp_path)
    path = root / "cases.json"
    cases = json.loads(path.read_text())
    cases["cases"][0]["evidence"][0]["sha256"] = "not-the-fixed-source"
    path.write_text(json.dumps(cases))
    with pytest.raises(ValueError, match="fixed corpus"):
        bench.inputs(SimpleNamespace(inputs=root))
