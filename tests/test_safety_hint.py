import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.safety_hint import build_safety_hint


def test_reservation_safety_hint():
    hint = build_safety_hint("Réserve-moi un restaurant pour ce soir", ["repas_et_restaurants", "delegation_et_validation"])
    assert hint.action == "poser_question"
