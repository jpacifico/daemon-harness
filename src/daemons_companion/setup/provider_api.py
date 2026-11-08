"""Provider checks for the app, with failures that name the provider.

The calls are daemon_core's own. Only the message changes: "the provider" becomes
its name, so the user knows which service failed, and the app can consult that
service's status page. Display only; nothing here decides a retry.
"""

from daemon_core import api_providers as core
from daemon_core.api_providers import ApiConnectionError

# The core's messages about its provider, by exact text, with what is kept and
# what to do. Others (an incomplete answer, an unusable model) are unchanged.
NAMED = {
    "The provider is unavailable. Try again later.":
        "{provider} is not answering right now. Your key is kept: try again in a few minutes.",
    "Cannot reach the provider. Check your internet connection and try again.":
        "Cannot reach {provider}. Your key is kept: check your Internet connection, then try again.",
    "The provider returned an invalid response. Try again later.":
        "{provider} returned an unexpected response. Your key is kept: try again in a few minutes.",
    "The provider reports a usage limit. Check your API balance or try again later.":
        "{provider} reports a usage limit. Check your API balance with {provider}, or try again later.",
    "The provider rejected this API key or its permissions. Check the key and try again.":
        "{provider} rejected this API key or its permissions. Check the key and try again.",
}


def named(kind: str, error: ApiConnectionError) -> ApiConnectionError:
    spec = core.API_PROVIDERS.get(kind)
    template = NAMED.get(str(error))
    return ApiConnectionError(template.format(provider=spec.label)) if spec and template else error


def model_catalog(kind, key, **options):
    try:
        return core.model_catalog(kind, key, **options)
    except ApiConnectionError as error:
        raise named(kind, error) from None


def verify_model(kind, key, model, **options):
    try:
        return core.verify_model(kind, key, model, **options)
    except ApiConnectionError as error:
        raise named(kind, error) from None
