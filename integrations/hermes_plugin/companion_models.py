"""Read the signed-in account's live catalog inside the private Hermes process.

Use Hermes' own credential resolver, account headers and catalog endpoints.
Export only display metadata. No credential refresh, source edits or inference.
"""
from __future__ import annotations

import json
import logging
import sys


def read_catalog():
    import httpx
    from hermes_cli.auth import resolve_codex_runtime_credentials, _codex_access_token_is_expiring
    from agent.codex_headers import codex_account_headers
    from agent.model_metadata import fetch_codex_catalog_entries

    token = resolve_codex_runtime_credentials(read_only=True).get('api_key')
    if not token or _codex_access_token_is_expiring(token, 0):
        raise RuntimeError('Sign in again before loading models.')
    headers = {'Authorization': f'Bearer {token}', **codex_account_headers(token)}
    entries, status = fetch_codex_catalog_entries(
        lambda url: httpx.get(url, headers=headers, timeout=10)
    )
    if status != 200 or not entries:
        raise RuntimeError('No live catalog was returned.')
    # Do not add host-only aliases or the native offline/forward-compat list:
    # the same exact slug must also be usable by the background Codex runtime.
    return {'models': [
        {key: item.get(key) for key in ('slug', 'display_name', 'description', 'visibility', 'priority')}
        for item in entries if isinstance(item, dict)
    ]}


def main():
    logging.disable(logging.CRITICAL)
    try:
        result = read_catalog()
    except Exception:
        # Native exception messages may carry HTTP authentication details.
        print('The account model list could not be loaded.', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
