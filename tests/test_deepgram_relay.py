"""Mock WebSocket relay protocol and security; no provider calls."""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask, session
import websocket

from app.routes.deepgram_stream import MAX_FRAME_BYTES, _relay


class Socket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
    def receive(self, timeout=None):
        return self.messages.pop(0) if self.messages else None
    def send(self, data):
        self.sent.append(json.loads(data))


class Upstream:
    def __init__(self):
        self.sent = []
        self.events = [json.dumps({'type': 'Results', 'start': 0, 'is_final': True,
                                   'channel': {'alternatives': [{'transcript': 'A complete answer.'}]}}),
                       json.dumps({'type': 'Metadata'})]
        self.closed = False
    def settimeout(self, timeout):
        pass
    def send_binary(self, data):
        self.sent.append(data)
    def send(self, data):
        self.sent.append(json.loads(data))
    def recv(self):
        return self.events.pop(0)
    def close(self):
        self.closed = True


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        self.service = SimpleNamespace(authorize=Mock(), get=Mock(return_value={'turn_id': 'turn', 'status': 'ready', 'paused': False}))
        self.app.extensions['conversation'] = self.service

    def relay(self, socket, origin='http://localhost', csrf='valid', upstream=None):
        with self.app.test_request_context('/api/voice/stream', headers={'Origin': origin}), \
             patch.dict('os.environ', {'STT_PROVIDER': 'deepgram', 'DEEPGRAM_API_KEY': 'test-secret'}), \
             patch('app.routes.deepgram_stream.websocket.create_connection', return_value=upstream or Upstream()) as connect:
            session['_csrf_token'] = csrf
            session['voice_interview_id'] = 'interview'
            _relay(socket)
            return connect

    def test_authenticated_stream_relays_final_transcript(self):
        socket = Socket([json.dumps({'csrf': 'valid', 'turn_id': 'turn'}), b'audio', json.dumps({'type': 'finish'})])
        upstream = Upstream()
        connection = self.relay(socket, upstream=upstream)
        self.assertTrue(connection.called)
        self.assertEqual(socket.sent[-1], {'type': 'complete', 'transcript': 'A complete answer.'})
        self.assertEqual(upstream.sent, [b'audio', {'type': 'CloseStream'}])
        self.assertTrue(upstream.closed)
        self.service.authorize.assert_called_once()

    def test_invalid_origin_or_csrf_does_not_open_provider(self):
        for origin, csrf in [('http://attacker.invalid', 'valid'), ('http://localhost', 'wrong')]:
            socket = Socket([json.dumps({'csrf': csrf, 'turn_id': 'turn'})])
            self.relay(socket, origin=origin).assert_not_called()
            self.assertEqual(socket.sent[0]['type'], 'error')

    def test_stale_turn_does_not_open_provider(self):
        socket = Socket([json.dumps({'csrf': 'valid', 'turn_id': 'stale'})])
        self.relay(socket).assert_not_called()
        self.assertEqual(socket.sent[0]['type'], 'error')

    def test_unauthorized_interview_does_not_open_provider(self):
        self.service.authorize.side_effect = RuntimeError('Access denied')
        socket = Socket([json.dumps({'csrf': 'valid', 'turn_id': 'turn'})])
        self.relay(socket).assert_not_called()
        self.assertEqual(socket.sent[0]['type'], 'error')

    def test_late_final_is_included_before_close_metadata(self):
        socket = Socket([json.dumps({'csrf': 'valid', 'turn_id': 'turn'}), b'first', json.dumps({'type': 'finish'})])
        upstream = Upstream()
        upstream.events.insert(1, json.dumps({'type': 'Results', 'start': 2, 'is_final': True,
                                              'channel': {'alternatives': [{'transcript': 'Late words.'}]}}))
        self.relay(socket, upstream=upstream)
        self.assertEqual(socket.sent[-1], {'type': 'complete', 'transcript': 'A complete answer. Late words.'})

    def test_oversized_frame_closes_provider_without_submitting(self):
        socket = Socket([json.dumps({'csrf': 'valid', 'turn_id': 'turn'}), b'x' * (MAX_FRAME_BYTES + 1)])
        upstream = Upstream()
        self.relay(socket, upstream=upstream)
        self.assertEqual(upstream.sent, [])
        self.assertTrue(upstream.closed)
        self.assertEqual(socket.sent[-1]['type'], 'error')

    def test_disconnection_without_flush_confirmation_never_emits_complete(self):
        socket = Socket([json.dumps({'csrf': 'valid', 'turn_id': 'turn'}), b'audio'])
        upstream = Upstream()
        upstream.events = []
        self.relay(socket, upstream=upstream)
        self.assertTrue(upstream.closed)
        self.assertFalse(any(item['type'] == 'complete' for item in socket.sent))
        self.assertEqual(socket.sent[-1]['type'], 'error')

    def test_slow_database_check_does_not_block_audio_finalization(self):
        gate = threading.Event()
        checking = threading.Event()
        ready = {'turn_id': 'turn', 'status': 'ready', 'paused': False}
        def get_state(identity):
            if self.service.get.call_count > 1:
                checking.set()
                gate.wait(5)
            return ready
        self.service.get.side_effect = get_state

        class SlowSocket(Socket):
            def receive(self, timeout=None):
                value = super().receive(timeout)
                if value == b'first':
                    time.sleep(1.1)
                elif value == b'second':
                    self_test.assertTrue(checking.wait(1))
                return value

        class WaitingUpstream(Upstream):
            def __init__(self):
                super().__init__()
                self.events = []
            def send(self, data):
                super().send(data)
                self.events.append(json.dumps({'type': 'Metadata'}))
            def recv(self):
                if self.events:
                    return self.events.pop(0)
                raise websocket.WebSocketTimeoutException()

        self_test = self
        socket = SlowSocket([json.dumps({'csrf': 'valid', 'turn_id': 'turn'}), b'first', b'second', json.dumps({'type': 'finish'})])
        try:
            self.relay(socket, upstream=WaitingUpstream())
            self.assertEqual(socket.sent[-1], {'type': 'complete', 'transcript': ''})
            self.assertFalse(gate.is_set())
        finally:
            gate.set()
