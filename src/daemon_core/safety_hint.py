from dataclasses import dataclass


@dataclass
class SafetyHint:
    action: str
    reason: str


def build_safety_hint(message: str, selected_souvenirs: list[str]) -> SafetyHint:
    if "delegation_et_validation" in selected_souvenirs:
        return SafetyHint(action="poser_question", reason="Contexte suffisant pour avancer, mais insuffisant pour réserver sans précisions ni validation.")
    return SafetyHint(action="respondre", reason="Contexte suffisant pour une réponse réflexe.")
