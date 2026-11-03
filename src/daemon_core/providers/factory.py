import logging
import os
from daemons_companion.setup.provider_runtime import managed_provider_config
from .base import LLMProvider
from .telemetry import instrument_provider

logger = logging.getLogger("daemon_core.providers")

# Reject minimal reasoning: an observed run (2026-07-08) returned
# the candidates without judgment in 0.2 seconds.
VALID_REASONING_EFFORTS = {"low", "medium", "high"}
DEFAULT_ROUTING_REASONING_EFFORT = "medium"
DEFAULT_CONVERSATION_REASONING_EFFORT = "medium"
DEFAULT_MEMORY_REASONING_EFFORT = "high"


def build_provider(
    provider_env: str = "LLM_PROVIDER",
    openai_model_env: str = "OPENAI_MODEL",
    local_model_env: str = "LOCAL_LLM_MODEL",
    codex_model_env: str = "CODEX_CLI_MODEL",
    effort_env: str = "LLM_REASONING_EFFORT",
    default_reasoning_effort: str | None = None,
    faculty: str = "generic",
) -> LLMProvider:
    managed = managed_provider_config()

    def setting(name: str, fallback: str = "") -> str:
        if managed is not None and managed.get(name) is not None:
            return str(managed.get(name))
        return os.getenv(name, fallback)

    provider = setting(provider_env, setting("LLM_PROVIDER", "echo")).lower().strip()
    effort = _reasoning_effort(
        effort_env,
        default=default_reasoning_effort,
        configured=managed.get(effort_env) if managed is not None else None,
    )
    if provider in {"mistral", "anthropic"}:
        from .mistral_provider import MistralProvider
        from .anthropic_provider import AnthropicProvider

        model_key = provider.upper() + "_MODEL"
        faculty_key = provider_env.removesuffix("LLM_PROVIDER") + model_key
        secret_key = provider.upper() + "_API_KEY"
        constructor = MistralProvider if provider == "mistral" else AnthropicProvider
        built = constructor(model=setting(faculty_key) or setting(model_key),
                            reasoning_effort=effort,
                            api_key=managed.secret(secret_key) if managed is not None else None)
        return instrument_provider(built, faculty=faculty)
    if provider == "xai":
        from .xai_provider import XaiProvider

        built = XaiProvider(model=setting(provider_env.removesuffix("LLM_PROVIDER") + "XAI_MODEL") or setting("XAI_MODEL"),
                            reasoning_effort=effort,
                            api_key=managed.secret("XAI_API_KEY") if managed is not None else None)
        return instrument_provider(built, faculty=faculty)
    if provider == "openai":
        from .openai_provider import OpenAIProvider

        built = OpenAIProvider(
            model=setting(openai_model_env) or setting("OPENAI_MODEL"),
            reasoning_effort=effort,
            api_key=(managed.secret("OPENAI_API_KEY") if managed is not None else None),
        )
        return instrument_provider(built, faculty=faculty)
    if provider == "local":
        from .local_openai_compatible_provider import LocalOpenAICompatibleProvider

        built = LocalOpenAICompatibleProvider(
            model=setting(local_model_env) or setting("LOCAL_LLM_MODEL"),
            reasoning_effort=effort,
            base_url=setting("LOCAL_LLM_BASE_URL") or None,
            api_key=(managed.secret("LOCAL_LLM_API_KEY") if managed is not None else None),
        )
        return instrument_provider(built, faculty=faculty)
    if provider in {"codex", "codex_cli"}:
        from .codex_cli_provider import CodexCliProvider

        built = CodexCliProvider(
            model=setting(codex_model_env) or setting("CODEX_CLI_MODEL"),
            reasoning_effort=effort,
        )
        return instrument_provider(built, faculty=faculty)
    if provider == "echo":
        from .echo_provider import EchoProvider
        return instrument_provider(EchoProvider(), faculty=faculty)
    raise ValueError(f"LLM_PROVIDER inconnu: {provider}")


def _reasoning_effort(
    effort_env: str,
    *,
    default: str | None = None,
    configured: str | None = None,
) -> str | None:
    value = (configured if configured is not None else os.getenv(effort_env, "")).lower().strip()
    if not value:
        return default
    if value not in VALID_REASONING_EFFORTS:
        logger.warning(
            "%s=%s ignoré ; défaut=%s (valeurs valides : %s)",
            effort_env,
            value,
            default or "natif",
            "/".join(sorted(VALID_REASONING_EFFORTS)),
        )
        return default
    return value


def build_routing_provider() -> LLMProvider:
    return build_provider(
        provider_env="ROUTING_LLM_PROVIDER",
        openai_model_env="ROUTING_OPENAI_MODEL",
        local_model_env="ROUTING_LOCAL_LLM_MODEL",
        codex_model_env="ROUTING_CODEX_MODEL",
        effort_env="ROUTING_REASONING_EFFORT",
        default_reasoning_effort=DEFAULT_ROUTING_REASONING_EFFORT,
        faculty="routing",
    )


def build_conversation_provider() -> LLMProvider:
    return build_provider(
        provider_env="CONVERSATION_LLM_PROVIDER",
        openai_model_env="CONVERSATION_OPENAI_MODEL",
        local_model_env="CONVERSATION_LOCAL_LLM_MODEL",
        codex_model_env="CONVERSATION_CODEX_MODEL",
        effort_env="CONVERSATION_REASONING_EFFORT",
        default_reasoning_effort=DEFAULT_CONVERSATION_REASONING_EFFORT,
        faculty="conversation",
    )


def build_memory_provider() -> LLMProvider:
    return build_provider(
        provider_env="MEMORY_LLM_PROVIDER",
        openai_model_env="MEMORY_OPENAI_MODEL",
        local_model_env="MEMORY_LOCAL_LLM_MODEL",
        codex_model_env="MEMORY_CODEX_MODEL",
        effort_env="MEMORY_REASONING_EFFORT",
        default_reasoning_effort=DEFAULT_MEMORY_REASONING_EFFORT,
        faculty="memory",
    )
