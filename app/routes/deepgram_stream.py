"""Authenticated browser-to-Deepgram WebSocket relay (experimental)."""
import hmac
import json
import os
import threading
import time
from urllib.parse import urlencode, urlsplit

import websocket
from flask import current_app, request, session
from flask_sock import Sock
from simple_websocket import ConnectionClosed

from app.services.deepgram_transcript import DeepgramTranscript
from app.voice_config import voice_config

sock = Sock()
_slots = threading.BoundedSemaphore(max(1, int(os.getenv('STREAM_MAX_CONNECTIONS_PER_WORKER', '2'))))
MAX_FRAME_BYTES = 256 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024


def _send(ws, kind, **fields):
    ws.send(json.dumps({'type': kind, **fields}))


def _valid_origin(origin, host):
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme in {'http', 'https'} and parsed.netloc == host
                and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def _relay(ws):
    # Browser WebSocket requests do not support custom CSRF headers. Require
    # same-origin AND the existing session's CSRF token in the first message.
    upstream = None
    acquired = False
    try:
        if os.getenv('STT_PROVIDER', 'whisper').strip().lower() != 'deepgram':
            _send(ws, 'error', message='Streaming is unavailable.')
            return
        origin = request.headers.get('Origin')
        if not _valid_origin(origin, request.host) or (os.getenv('APP_ENV') == 'production' and urlsplit(origin).scheme != 'https'):
            _send(ws, 'error', message='Streaming is unavailable.')
            return
        initial = ws.receive(timeout=5)
        try:
            hello = json.loads(initial) if isinstance(initial, str) and len(initial) <= 4096 else {}
        except ValueError:
            hello = {}
        csrf = hello.get('csrf') if isinstance(hello, dict) else None
        expected = session.get('_csrf_token')
        if not isinstance(csrf, str) or not isinstance(expected, str) or not hmac.compare_digest(csrf, expected):
            _send(ws, 'error', message='Streaming is unavailable.')
            return
        identity = session.get('voice_interview_id')
        turn_id = hello.get('turn_id')
        service = current_app.extensions['conversation']
        if not identity or not isinstance(turn_id, str):
            _send(ws, 'error', message='Streaming is unavailable.')
            return
        service.authorize(identity, session)
        current = service.get(identity)
        if current['turn_id'] != turn_id or current['status'] != 'ready' or current.get('paused') or current.get('end_requested'):
            _send(ws, 'error', message='This interview turn is no longer available.')
            return
        key = os.getenv('DEEPGRAM_API_KEY', '').strip()
        if not key or not _slots.acquire(blocking=False):
            _send(ws, 'error', message='Streaming is unavailable.')
            return
        acquired = True
        params = urlencode({'model': os.getenv('DEEPGRAM_STT_MODEL', 'nova-3'), 'language': 'en',
                            'interim_results': 'true', 'vad_events': 'true', 'endpointing': '400',
                            'utterance_end_ms': '5000', 'smart_format': 'true'})
        settings = voice_config()
        upstream = websocket.create_connection('wss://api.deepgram.com/v1/listen?' + params,
                                               header=['Authorization: Token ' + key], timeout=settings['DEEPGRAM_CONNECT_TIMEOUT_SECONDS'])
        upstream.settimeout(.05)
        _send(ws, 'ready')
        transcript = DeepgramTranscript()
        started = time.monotonic()
        next_check = started + 1
        received = 0
        closing = False
        close_deadline = None
        while time.monotonic() - started < settings['MAX_CAPTURE_SECONDS']:
            now = time.monotonic()
            if close_deadline is not None and now >= close_deadline:
                raise TimeoutError('Final transcript was not confirmed.')
            if now >= next_check:
                service.authorize(identity, session)
                current = service.get(identity)
                if current['turn_id'] != turn_id or current['status'] != 'ready' or current.get('paused') or current.get('end_requested'):
                    _send(ws, 'error', message='This interview turn is no longer available.')
                    break
                next_check = now + 1
            incoming = ws.receive(timeout=.05)
            if isinstance(incoming, bytes):
                if closing or not incoming or len(incoming) > MAX_FRAME_BYTES:
                    raise ValueError('Invalid audio frame.')
                received += len(incoming)
                if received > MAX_STREAM_BYTES:
                    raise ValueError('Stream too large.')
                upstream.send_binary(incoming)
            elif isinstance(incoming, str):
                command = json.loads(incoming)
                if command == {'type': 'finish'} and not closing:
                    upstream.send(json.dumps({'type': 'CloseStream'}))
                    closing = True
                    close_deadline = time.monotonic() + settings['DEEPGRAM_FINALIZE_TIMEOUT_SECONDS']
                else:
                    raise ValueError('Invalid stream command.')
            try:
                message = upstream.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not message:
                raise ConnectionError('Speech stream closed unexpectedly.')
            event = json.loads(message)
            event_type = event.get('type')
            if event_type == 'Results':
                recognized = transcript.accept(event)
                if len(transcript.transcript) + len(transcript.interim) > settings['MAX_ANSWER_CHARACTERS']:
                    raise ValueError('Transcript exceeds the configured limit.')
                alternatives = event.get('channel', {}).get('alternatives', [])
                confidence = alternatives[0].get('confidence') if alternatives and isinstance(alternatives[0], dict) else None
                confident = isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and settings['DEEPGRAM_COMMAND_MIN_CONFIDENCE'] <= confidence <= 1
                _send(ws, 'transcript', transcript=transcript.transcript, interim=transcript.interim,
                      recognized=recognized, final=event.get('is_final') is True, command_confident=confident)
            elif event_type == 'Metadata' and closing:
                _send(ws, 'complete', transcript=transcript.transcript)
                return
        _send(ws, 'error', message='Speech streaming ended. Please retry the answer.')
    except (ConnectionClosed, websocket.WebSocketConnectionClosedException):
        pass
    except Exception:
        # Provider messages may contain sensitive headers; never log them.
        try:
            _send(ws, 'error', message='Speech streaming failed. Please retry the answer.')
        except Exception:
            pass
    finally:
        if upstream:
            try:
                upstream.close()
            except Exception:
                pass
        if acquired:
            _slots.release()


@sock.route('/api/voice/stream')
def stream(ws):
    _relay(ws)
