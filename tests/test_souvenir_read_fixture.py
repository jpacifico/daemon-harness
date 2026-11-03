from scripts import souvenir_read_fixture as fixture


def terminal():
    return {'choices': [{'message': {'role': 'assistant', 'content': 'done'},
                         'finish_reason': 'stop'}]}


def test_fixture_only_requests_the_new_native_tool_when_exposed():
    request = {'tools': [{'function': {'name': 'daemon_read_souvenirs'}}]}
    response = fixture.completion(request, terminal(), enabled=True)
    assert response['choices'][0]['finish_reason'] == 'tool_calls'
    assert response['choices'][0]['message']['tool_calls'][0]['function']['name'] == 'daemon_read_souvenirs'
    assert fixture.completion(request, terminal(), enabled=False) == terminal()
    assert fixture.completion({}, terminal(), enabled=True) == terminal()


def test_fixture_stops_only_after_an_exact_native_tool_result():
    request = {'tools': [{'function': {'name': 'daemon_read_souvenirs'}}],
               'messages': [{'role': 'tool', 'content': fixture.MARKER}]}
    assert fixture.tool_round_trip_seen([request])
    assert fixture.completion(request, terminal(), enabled=True) == terminal()


def test_fixture_seed_preserves_historical_disk_format(tmp_path):
    fixture.seed(tmp_path)
    assert fixture.MARKER in (tmp_path / 'capsules' / (fixture.PATH + '.md')).read_text()
    assert not (tmp_path / 'souvenirs').exists()
