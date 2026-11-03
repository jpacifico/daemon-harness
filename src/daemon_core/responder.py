"""Convert exact L0 dialogue into native chat turns; no response engine."""




def _dialogue_to_turns(
    recent_dialogue: str,
    *,
    preserve_timestamps: bool = True,
) -> list[dict]:
    """Convertit le L0 en tours de chat sans perdre leur provenance temporelle."""
    turns: list[dict] = []
    role, timestamp, lines = None, "", []

    def append_turn() -> None:
        content = "\n".join(lines).strip()
        if not role or not content:
            return
        if preserve_timestamps and timestamp:
            content = f"[heure locale du tour : {timestamp}]\n{content}"
        turns.append({"role": role, "content": content})

    for line in recent_dialogue.splitlines():
        header = line.startswith("## ") and " — " in line
        if header:
            append_turn()
            raw_role = line.rsplit(" — ", 1)[-1].strip().lower()
            timestamp = line[3:].rsplit(" — ", 1)[0].strip()
            role, lines = (
                "assistant" if raw_role == "assistant" else "user"
            ), []
        elif role is not None:
            lines.append(line)
    append_turn()
    return turns
