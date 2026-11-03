"""Private prompt annotations that must never become visible speech."""

from __future__ import annotations

import re


TURN_TIME_PREFIX = "[heure locale du tour : "
TURN_COMMITMENT_PREFIX = "[daemon-turn"
PROTOCOL_LEADING_WHITESPACE = " \t\r\n\ufeff"
TURN_TIME_LINE_RE = re.compile(
    r"(?m)^[ \t]*\[heure locale du tour : "
    r"\d{4}-\d{2}-\d{2}T[^\]\r\n]+\][ \t]*(?:\r?\n|$)"
)
TURN_COMMITMENT_LINE_RE = re.compile(
    r"\A[ \t\r\n\ufeff]*\[daemon-turn[^\r\n]*(?:\r?\n|$)"
)
TURN_COMMITMENT_RE = re.compile(
    r"\A[ \t\r\n\ufeff]*\[daemon-turn:[ \t]*(exchange|direct|clarify|condition)[ \t]*\]"
    r"[ \t]*(.*?)[ \t]*(?:\r?\n|$)",
    re.IGNORECASE,
)


def strip_internal_turn_metadata(text: str) -> str:
    """Apply a model-authored turn commitment and hide private metadata."""

    if not text:
        return text
    without_time = TURN_TIME_LINE_RE.sub("", text)
    commitments, cleaned = _consume_leading_turn_commitments(without_time)
    commitment = next(
        (item for item in commitments if item[1]),
        commitments[0] if commitments else None,
    )
    if commitment is not None and commitment[0] == "clarify" and commitment[1]:
        return commitment[1]
    metadata_removed = without_time != text or cleaned != without_time
    delivered = cleaned.lstrip("\r\n") if metadata_removed else text
    if (
        commitment is not None
        and commitment[0] == "condition"
        and commitment[1]
    ):
        return _join_condition(commitment[1], delivered)
    if (
        commitment is not None
        and commitment[0] in {"exchange", "direct"}
        and commitment[1]
    ):
        return _join_condition(commitment[1], delivered)
    return delivered


class TurnMetadataStreamFilter:
    """Hold possible leading metadata lines until they can be classified."""

    def __init__(self) -> None:
        self._pending = ""
        self._decided = False
        self._suppress_rest = False
        self._condition_movement = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self._suppress_rest:
            return ""
        if self._decided:
            return chunk
        if self._condition_movement:
            self._pending += chunk
            if not self._pending.strip():
                return ""
            movement = self._condition_movement
            self._condition_movement = ""
            chunk = self._pending
            self._pending = ""
            self._decided = True
            return _join_condition(movement, chunk)

        self._pending += chunk
        while True:
            candidate = self._pending.lstrip(PROTOCOL_LEADING_WHITESPACE)
            if not candidate:
                if len(self._pending) > 2048:
                    return self._finish_pending()
                return ""
            folded = candidate.casefold()
            possible_metadata = any(
                prefix.casefold().startswith(folded)
                or folded.startswith(prefix.casefold())
                for prefix in (TURN_TIME_PREFIX, TURN_COMMITMENT_PREFIX)
            )
            if possible_metadata and candidate != self._pending:
                # A provider or tool boundary may prepend a standalone blank
                # delta before the model's private first line. Do not let that
                # harmless transport whitespace make the protocol public.
                self._pending = candidate
            line_end = _first_line_end(self._pending)
            if line_end is not None:
                line = self._pending[:line_end]
                if _is_internal_metadata_line(line):
                    commitment = _parse_turn_commitment(line)
                    self._pending = self._pending[line_end:]
                    if (
                        commitment is not None
                        and commitment[0] == "clarify"
                        and commitment[1]
                    ):
                        self._pending = ""
                        self._decided = True
                        self._suppress_rest = True
                        return commitment[1]
                    if (
                        commitment is not None
                        and commitment[0] == "condition"
                        and commitment[1]
                    ):
                        if self._pending:
                            pending = self._pending
                            self._pending = ""
                            self._decided = True
                            return _join_condition(commitment[1], pending)
                        self._condition_movement = commitment[1]
                    if (
                        commitment is not None
                        and commitment[0] in {"exchange", "direct"}
                        and commitment[1]
                    ):
                        inline = commitment[1]
                        if self._pending:
                            pending = self._pending
                            self._pending = ""
                            self._decided = True
                            return _join_condition(inline, pending)
                        self._decided = True
                        return inline
                    if not self._pending:
                        return ""
                    continue
                return self._finish_pending()
            if not possible_metadata:
                return self._finish_pending()
            if len(self._pending) > 2048:
                return self._finish_pending()
            return ""

    def finish(self) -> str:
        if self._suppress_rest or self._decided:
            return ""
        if self._condition_movement:
            movement = self._condition_movement
            self._condition_movement = ""
            self._decided = True
            return movement
        if not self._pending:
            self._decided = True
            return ""
        return self._finish_pending()

    def _finish_pending(self) -> str:
        pending = self._pending
        self._pending = ""
        self._decided = True
        return strip_internal_turn_metadata(pending)


def _first_line_end(text: str) -> int | None:
    indexes = [index for index in (text.find("\n"), text.find("\r")) if index >= 0]
    if not indexes:
        return None
    index = min(indexes)
    if text[index:index + 2] == "\r\n":
        return index + 2
    return index + 1


def _is_internal_metadata_line(line: str) -> bool:
    return bool(
        TURN_TIME_LINE_RE.fullmatch(line)
        or TURN_COMMITMENT_LINE_RE.fullmatch(line)
    )


def _join_condition(movement: str, delivered: str) -> str:
    body = delivered.lstrip("\r\n")
    if not body:
        return movement
    if body.startswith(movement):
        return body
    return f"{movement}\n{body}"


def _parse_turn_commitment(text: str) -> tuple[str, str] | None:
    match = TURN_COMMITMENT_RE.match(text)
    if match is None:
        return None
    return match.group(1).lower(), match.group(2).strip()


def _consume_leading_turn_commitments(
    text: str,
) -> tuple[list[tuple[str, str]], str]:
    commitments: list[tuple[str, str]] = []
    remaining = text
    while match := TURN_COMMITMENT_LINE_RE.match(remaining):
        commitment = _parse_turn_commitment(match.group(0))
        if commitment is not None:
            commitments.append(commitment)
        remaining = remaining[match.end():]
    return commitments, remaining
