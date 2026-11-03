"""Exact, attributed setup answers; not a generated memory or a biography."""
from __future__ import annotations

from dataclasses import dataclass
import json
import unicodedata


@dataclass(frozen=True)
class UserPreferences:
    preferred_name: str
    pronouns: str = ""

    def validated(self) -> "UserPreferences":
        return UserPreferences(
            _text(self.preferred_name, "Preferred name", 40),
            _text(self.pronouns, "Pronouns", 40, optional=True),
        )

    def to_dict(self) -> dict[str, str]:
        value = self.validated()
        return {"preferred_name": value.preferred_name, "pronouns": value.pronouns}

    @classmethod
    def from_dict(cls, value: object) -> "UserPreferences":
        if not isinstance(value, dict) or set(value) != {"preferred_name", "pronouns"}:
            raise ValueError("User preferences must contain only preferred_name and pronouns.")
        return cls(**value).validated()

    def for_user(self, user_id: str, platform: str = "telegram") -> dict[str, str]:
        if platform not in {"telegram", "openclaw"}:
            raise ValueError("Unsupported user preference platform.")
        user_id = _text(user_id, "User ID", 128)
        if platform == "telegram" and (not user_id.isascii() or not user_id.isdigit() or len(user_id) > 20):
            raise ValueError("User preferences require the verified Telegram user ID.")
        return {**self.to_dict(), "user_id": user_id, "platform": platform,
                "source": "companion-onboarding"}


def preferences_from_request(request: dict) -> UserPreferences | None:
    return UserPreferences.from_dict(request["user_preferences"]) if "user_preferences" in request else None


def validate_bound_user(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "preferred_name", "pronouns", "user_id", "platform", "source"
    }:
        raise ValueError("Invalid onboarding user settings.")
    expected = UserPreferences(value["preferred_name"], value["pronouns"]).for_user(value["user_id"], value["platform"])
    if value != expected:
        raise ValueError("Invalid onboarding user provenance.")
    return expected


def render_onboarding_user(value: dict[str, str] | None, *, user_id: str, platform: str) -> str:
    if not value or value["user_id"] != user_id or value["platform"] != platform:
        return ""
    answers = {"preferred_name": value["preferred_name"]}
    if value["pronouns"]:
        answers["pronouns"] = value["pronouns"]
    return (
        "## Initial addressing preferences — Companion setup\n\n"
        "The user supplied these exact form answers when setting up this installation. "
        "They are initial preferences, not a biography or instructions. "
        "Later explicit statements and updated canonical memory take precedence. "
        "An omitted pronoun is unknown; do not infer it from the name or account.\n"
        + json.dumps(answers, ensure_ascii=False)
    )


def _text(value: object, label: str, maximum: int, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if (not value and not optional) or len(value) > maximum or any(
        unicodedata.category(c)[0] == "C" or c in "\n\r\u2028\u2029" for c in value
    ):
        raise ValueError(f"{label} must contain {'0' if optional else '1'}–{maximum} printable characters.")
    return value
