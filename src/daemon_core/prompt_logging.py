import logging


def log_llm_prompt(logger: logging.Logger, title: str, messages: list[dict]) -> None:
    lines = [
        "",
        "********************************************************************************",
        f"**** LLM PROMPT — {title}",
        "********************************************************************************",
    ]
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role", "unknown")).upper()
        content = str(message.get("content", ""))
        lines.extend(
            [
                "",
                f"---- MESSAGE {index} / ROLE={role} ----",
                content,
            ]
        )
    lines.extend(
        [
            "",
            "********************************************************************************",
            f"**** END LLM PROMPT — {title}",
            "********************************************************************************",
        ]
    )
    logger.info("\n".join(lines))
