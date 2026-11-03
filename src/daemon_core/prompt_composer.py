"""Shared clock text and scene rendering for the Hermes compatibility path.

The historical composite response prompt is retired; canonical voice owns
active conversation composition.
"""
from datetime import datetime

from daemon_core.temporal_frame import (
    resolve_reference_time,
)


def build_scene_section(
    active_request: str,
    ambiguity: str,
    ambiguity_reason: str = "",
    scene_register: str = "pratique",
    fil_clos: bool = False,
    scene_established: list[str] | None = None,
    scene_uncertain: list[str] | None = None,
    scene_decision_effect: str = "",
    scene_resolution_mode: str = "direct",
) -> str:
    request = active_request.strip()
    material = ambiguity == "material"
    established = [item.strip() for item in scene_established or [] if item.strip()]
    uncertain = [item.strip() for item in scene_uncertain or [] if item.strip()]
    decision_effect = scene_decision_effect.strip()
    resolution_mode = (
        scene_resolution_mode
        if scene_resolution_mode in {"direct", "conditional", "clarify"}
        else "direct"
    )
    register = scene_register if scene_register in {"pratique", "relationnel", "creatif", "taquin", "serieux", "tendu"} else "pratique"
    if not request and not material and register == "pratique" and not fil_clos and not established and not uncertain and not decision_effect:
        return ""
    lines = [
        "# Priority scene assessment",
        "",
        "This assessment guides the response but is not a source of facts.",
        "",
        f"Active request: {request or 'Infer cautiously from the scene.'}",
        f"Scene register: {register}",
        f"Material ambiguity: {'yes' if material else 'no'}",
    ]
    if established:
        lines.append("Useful established elements: " + " | ".join(established))
    if uncertain:
        lines.append("Elements to keep uncertain: " + " | ".join(uncertain))
    if decision_effect:
        lines.append(f"Effect on the decision: {decision_effect}")
    if established or uncertain or decision_effect:
        lines.append(f"Resolution strategy: {resolution_mode}")
    if material and ambiguity_reason.strip():
        lines.append(f"Reason: {ambiguity_reason.strip()}")
    if fil_clos:
        lines.append(
            "The current thread is winding down with no new request. Do not turn this "
            "closure into a forced initiative: respond in proportion to what has just "
            "been said, then leave room for silence. If a final echo genuinely adds "
            "something, a few words suffice; otherwise, simply "
            "conclude. Do not open another topic or add a new thesis "
            "to keep the exchange going."
        )
    return "\n".join(lines)


def current_time_context(now: datetime | None = None) -> str:
    current = resolve_reference_time(now)
    return current.strftime("%A %d %B %Y, %H:%M, Europe/Paris")
