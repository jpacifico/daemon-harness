"""Loopback Bot API and model fixtures; no real Telegram account or model.

The native Hermes Telegram adapter receives a synthetic numeric user, unlike
its API adapter whose memory identity is the profile name. All facts are fake.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import parse_qs

TOKEN = '123456789:' + 'A' * 35
USER_ID = 12345
BOT = {'id': 123456789, 'is_bot': True, 'first_name': 'Synthetic', 'username': 'synthetic_bot', 'can_join_groups': True, 'can_read_all_group_messages': False, 'supports_inline_queries': False}
USER = {'id': USER_ID, 'is_bot': False, 'first_name': 'Synthetic'}
CHAT = {'id': USER_ID, 'type': 'private', 'first_name': 'Synthetic'}


class TelegramFixture:
    def __init__(self):
        self.updates = []
        self.sent = []
        self.displayed = []
        self.submitted_at = None
        self.update_id = 0
        self.delivery_seconds = []
        self.methods = []
        self.unknown = []
        self.dropped_counts = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if 'application/json' in self.headers.get('Content-Type', ''):
                    values = json.loads(raw or '{}')
                else:
                    values = {key: entries[0] for key, entries in parse_qs(raw.decode()).items()}
                method = self.path.rsplit('/', 1)[-1].lower()
                assert self.path.startswith('/bot' + TOKEN + '/')
                with owner.lock:
                    owner.methods.append(method)
                result = True
                if method == 'getme':
                    result = BOT
                elif method == 'getwebhookinfo':
                    result = {'url': '', 'has_custom_certificate': False, 'pending_update_count': 0}
                elif method == 'deletewebhook':
                    if str(values.get('drop_pending_updates', '')).lower() == 'true':
                        with owner.lock:
                            owner.dropped_counts.append(len(owner.updates))
                            owner.updates.clear()
                elif method == 'getupdates':
                    owner.wake.wait(0.25)
                    with owner.lock:
                        offset = int(values.get('offset', 0))
                        owner.updates = [item for item in owner.updates if item['update_id'] >= offset]
                        result = list(owner.updates)
                        owner.wake.clear()
                elif method in {'sendmessage', 'editmessagetext'}:
                    text = values.get('text', '')
                    with owner.lock:
                        owner.sent.append(text)
                        visible = re.sub(r"\\(.)", r"\1", text) if values.get('parse_mode') == 'MarkdownV2' else text
                        owner.displayed.append(visible)
                        owner.delivery_seconds.append(None if owner.submitted_at is None else round(time.monotonic() - owner.submitted_at, 3))
                        message_id = len(owner.sent) + 10
                    result = {'message_id': message_id, 'date': int(time.time()), 'chat': CHAT, 'from': BOT, 'text': text}
                elif method == 'getchat':
                    result = {**CHAT, 'accent_color_id': 0, 'max_reaction_count': 1}
                elif method == 'getmycommands':
                    result = []
                elif method not in {'deletewebhook', 'sendchataction', 'deletemycommands', 'setmycommands', 'deletemessage', 'setmydescription', 'setmyshortdescription', 'setchatmenubutton', 'setmessagereaction'}:
                    with owner.lock:
                        owner.unknown.append(method)
                    self.respond({'ok': False, 'error_code': 400, 'description': 'Unsupported fixture method'})
                    return
                self.respond({'ok': True, 'result': result})

            def respond(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, text):
        with self.lock:
            self.submitted_at = time.monotonic()
            self.update_id += 1
            message = {'message_id': self.update_id, 'date': int(time.time()), 'chat': CHAT, 'from': USER, 'text': text}
            if text.startswith('/'):
                message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
            self.updates.append({'update_id': self.update_id, 'message': message})
        self.wake.set()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def model_completion(request, completion):
    """Valid structural responses for the one synthetic infrastructure exchange.

    This fixture does not evaluate model judgment. Production prompts, parsing,
    memory writes, transport, spool and receipts all execute unchanged.
    """
    messages = request.get('messages', [])
    system = '\n'.join(str(item.get('content', '')) for item in messages if item.get('role') == 'system')
    text = None
    if system.startswith("You are Daemon Harness's memory radar."):
        text = json.dumps({'context_depth': 'light', 'reason': 'Synthetic transport greeting without personal facts.', 'confidence': 1.0})
    elif system.startswith('You prepare the forensic bootstrap of conversational working memory.'):
        content = '\n'.join(str(item.get('content', '')) for item in messages if item.get('role') == 'user')
        selected = re.findall(r'^### (\S+)$', content, re.MULTILINE)
        assert len(selected) <= 4, 'The fixture only describes two synthetic exchanges'
        text = json.dumps({'selected': selected, 'reason': 'Both exact turns of the synthetic installer exchange.'})
    elif system.startswith('You maintain the Daemon Harness current scene model between turns.'):
        content = '\n'.join(str(item.get('content', '')) for item in messages if item.get('role') == 'user')
        marker = 'Units this judgment must explicitly account for:\n'
        units = json.JSONDecoder().raw_decode(content.split(marker, 1)[1])[0]
        text = json.dumps({'reading': 'A synthetic installer exchange.', 'conversation': 'The fixture has answered one test message.', 'changes': [], 'hot_memories': [], 'released_memories': [], 'dependency_changes': [], 'source_dispositions': [{'source': unit, 'condition': 'nonoperative', 'dependency': 'none', 'evidence_sources': [], 'reason': 'Infrastructure test without a personal fact or commitment.'} for unit in units]})
    elif system.startswith('You read the current texture of an ongoing conversation'):
        text = '# Conversation state\n\nOne synthetic installer exchange.'
    elif system.startswith('You are the Daemon Harness DreamingAgent.'):
        text = json.dumps({'reason': 'Synthetic greeting has no durable facts to change.',
                           'updates': [], 'report_md': 'No change required.'})
    elif system.startswith('You are the episodic memory curator of a personal daemon.'):
        text = json.dumps({'reason': 'Infrastructure greeting has no durable episode.',
                           'revisit_from': None, 'scenes': []})
    elif system.startswith('You are the narrator of Daemon Harness.'):
        text = 'The user is testing a fresh installation.'
    if text is not None:
        completion['choices'][0]['message']['content'] = text
    return completion
