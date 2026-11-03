from __future__ import annotations

import sqlite3

from integrations.openclaw_plugin.origin import resolve_delivery_origin


def test_resolve_delivery_origin_reads_only_native_routing(tmp_path):
    database = tmp_path / "openclaw-agent.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            account_id TEXT NOT NULL,
            delivery_target TEXT NOT NULL,
            peer_id TEXT NOT NULL,
            thread_id TEXT
        );
        CREATE TABLE session_windows (
            session_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            primary_conversation_id TEXT
        );
        INSERT INTO conversations VALUES (
            'conversation-1', 'telegram', 'default', 'telegram:12345', '12345', '77'
        );
        INSERT INTO session_windows VALUES (
            'session-1', 'agent:main:telegram:direct:12345', 'conversation-1'
        );
        """
    )
    connection.commit()
    connection.close()

    assert resolve_delivery_origin("session-1", database_path=database) == {
        "channel": "telegram",
        "to": "telegram:12345",
        "account_id": "default",
        "session_key": "agent:main:telegram:direct:12345",
        "thread_id": "77",
    }
    assert resolve_delivery_origin("missing", database_path=database) == {}
