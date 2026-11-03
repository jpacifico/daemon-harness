from pathlib import Path
import sys
import asyncio
import json
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from replay_web_boundary import PublicReplayWeb


@pytest.mark.parametrize('query', ['person secret', 'public name user@example.org', 'public name 12345', 'public name?token=secret', 'public\nunknown', 'public/name', 'public name&secret', 'public name\x00', ''])
def test_private_or_unregistered_query_never_reaches_native_tool(query):
    gate = PublicReplayWeb(['public', 'name'], lambda row: None)
    def forbidden(*a, **kw):
        raise AssertionError('Network boundary crossed')
    assert json.loads(gate.search(forbidden, query))['success'] is False


def test_approved_query_and_only_discovered_urls_reach_native_tool():
    calls=[]
    rows=[]
    gate=PublicReplayWeb(['public','name'], rows.append)
    url='https://example.org/public'
    def search(query, limit):
        calls.append(query)
        return json.dumps({'success':True,'data':{'web':[{'url':url}]}})
    assert json.loads(gate.search(search, 'Public name'))['success']
    assert calls==['Public name']
    async def extract(urls, format, char_limit):
        calls.extend(urls)
        return 'native result'
    assert asyncio.run(gate.extract(extract, [url], 'markdown'))=='native result'
    assert json.loads(asyncio.run(gate.extract(extract, [url+'?private=info'])))['success'] is False
    assert calls==['Public name',url]


def test_only_registered_public_site_operator_can_leave():
    gate = PublicReplayWeb(['public', 'name'], lambda row: None, ['site:example.org'])
    assert gate.permits_query('Public name site:example.org')
    assert not gate.permits_query('Public name site:private.example.org')
    assert not gate.permits_query('Public name site:example.org/path-secret')
    assert not gate.permits_query('Public name site:example.org?user=secret')


def test_unknown_query_cannot_leave_without_exact_operator_review():
    calls=[]
    def native(query, limit=5):
        calls.append(query)
        return json.dumps({'success': True})
    denied = PublicReplayWeb(['public'], lambda row: None, review=lambda q: False)
    assert not json.loads(denied.search(native, 'unregistered public topic'))['success']
    assert calls == []
    approved = PublicReplayWeb(['public'], lambda row: None, review=lambda q: q == 'unregistered public topic')
    assert json.loads(approved.search(native, 'unregistered public topic'))['success']
    assert calls == ['unregistered public topic']
    assert not json.loads(approved.search(native, 'private other topic'))['success']
    assert calls == ['unregistered public topic']
