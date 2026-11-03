"""Evaluation-only boundary: private prompt text cannot become a Web request.

The closed vocabulary belongs to the private test manifest. Unknown text stops
at the local boundary; tools in the product and active host are never patched.
"""
import json
import hashlib
import time
import re
from urllib.parse import urlsplit


class PublicReplayWeb:
    def __init__(self, public_words, record, public_operators=(), review=None):
        self.review = review
        self.words = frozenset(public_words)
        self.operators = tuple(public_operators)
        self.urls = set()
        self.record = record

    def permits_query(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 160:
            return False
        for operator in self.operators:
            query = re.sub(r'(?<!\S)' + re.escape(operator) + r'(?!\S)', '', query, flags=re.I)
        if not re.fullmatch(r'[A-Za-zÀ-ÿ\s"\-]+', query):
            return False
        tokens = re.findall(r'[A-Za-zÀ-ÿ]+', query.casefold())
        return bool(tokens) and len(tokens) <= 20 and set(tokens) <= self.words

    def search(self, native, query, limit=5):
        safe_limit = type(limit) is int and 1 <= limit <= 10
        permitted = self.permits_query(query) and safe_limit
        if not permitted and safe_limit and self.review is not None:
            permitted = self.review(query) is True
        self.record({'tool': 'web_search', 'query': query, 'permitted': permitted})
        if not permitted:
            return json.dumps({'success': False, 'error': 'Isolated evaluation: request outside approved public vocabulary; no network request was sent.'})
        result = native(query, limit=limit)
        try:
            rows = json.loads(result).get('data', {}).get('web', [])
            for row in rows:
                url = row.get('url', '')
                parts = urlsplit(url)
                if parts.scheme == 'https' and parts.netloc and not parts.username and not parts.password:
                    self.urls.add(url)
        except (ValueError, AttributeError, TypeError):
            pass
        return result

    async def extract(self, native, urls, format=None, char_limit=None):
        permitted = (
            isinstance(urls, list) and 1 <= len(urls) <= 5
            and all(isinstance(url, str) and url in self.urls for url in urls)
            and format in (None, 'markdown')
            and (char_limit is None or type(char_limit) is int and 2000 <= char_limit <= 50000)
        )
        self.record({'tool': 'web_extract', 'urls': urls, 'permitted': permitted})
        if not permitted:
            return json.dumps({'success': False, 'error': 'Isolated evaluation: extraction requires an exact public URL discovered by an approved search; no network request was sent.'})
        return await native(urls, format, char_limit=char_limit)


def install_public_web_boundary(manifest, log_path):
    from tools import web_tools

    def record(row):
        with log_path.open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    def review(query):
        if not isinstance(query, str) or not 1 <= len(query) <= 300:
            return False
        key = hashlib.sha256(query.encode()).hexdigest()
        request = log_path.parent / ("web-review-" + key + ".json")
        approval = log_path.parent / ("web-approved-" + key + ".json")
        request.write_text(json.dumps({"query": query, "sha256": key}, ensure_ascii=False) + "\n")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if approval.exists():
                try:
                    decision = json.loads(approval.read_text())
                except (OSError, ValueError):
                    return False
                return decision == {"query": query, "decision": "allow-public-query"}
            time.sleep(0.1)
        return False

    gate = PublicReplayWeb(manifest['public_web_words'], record, manifest.get('public_web_operators', ()), review if manifest.get('manual_public_review') else None)
    search, extract = web_tools.web_search_tool, web_tools.web_extract_tool
    web_tools.web_search_tool = lambda query, limit=5: gate.search(search, query, limit)

    async def wrapped_extract(urls, format=None, char_limit=None):
        return await gate.extract(extract, urls, format, char_limit)

    web_tools.web_extract_tool = wrapped_extract
    return gate
