"""Mocked provider tests; no speech-service or Gemini calls."""
import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
import run

from app.services import tts
from app.services.deepgram_tts import synthesize_deepgram


class DeepgramTTSTests(unittest.TestCase):
    def request(self, handler, text='Hello'):
        real_client = httpx.AsyncClient
        with patch.dict(os.environ, {'DEEPGRAM_API_KEY': 'test-secret'}, clear=False), \
             patch('app.services.deepgram_tts.httpx.AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            return asyncio.run(synthesize_deepgram(text))

    def test_request_and_audio_are_returned(self):
        def handler(request):
            self.assertEqual(str(request.url), 'https://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=mp3')
            self.assertEqual(request.headers['authorization'], 'Token test-secret')
            self.assertEqual(request.read(), b'{"text":"Hello"}')
            return httpx.Response(200, headers={'content-type': 'audio/mpeg'}, content=b'mock-mp3')
        self.assertEqual(self.request(handler), b'mock-mp3')

    def test_provider_errors_do_not_expose_secrets(self):
        with self.assertRaisesRegex(RuntimeError, 'synthesis failed') as raised:
            self.request(lambda _: httpx.Response(401, text='private test-secret'))
        self.assertNotIn('test-secret', str(raised.exception))
        with self.assertRaisesRegex(RuntimeError, 'invalid audio'):
            self.request(lambda _: httpx.Response(200, headers={'content-type': 'application/json'}, content=b'{}'))
        with self.assertRaisesRegex(RuntimeError, 'empty audio'):
            self.request(lambda _: httpx.Response(200, headers={'content-type': 'audio/mpeg'}, content=b''))

    def test_provider_selection_and_default(self):
        async def fake_deepgram(text):
            return text.encode()
        with patch.dict(os.environ, {'TTS_PROVIDER': 'deepgram'}), patch('app.services.deepgram_tts.synthesize_deepgram', fake_deepgram):
            self.assertEqual(asyncio.run(tts.synthesize_speech(' Example ')), b'Example')
        with patch.dict(os.environ, {'TTS_PROVIDER': 'invalid'}), self.assertRaisesRegex(ValueError, 'Unsupported'):
            asyncio.run(tts.synthesize_speech('Example'))

    def test_existing_synthesis_route_uses_selected_provider(self):
        async def fake_deepgram(text):
            return b'mock-mp3'
        with patch.dict(os.environ, {'TTS_PROVIDER': 'deepgram'}), \
             patch.dict(run.app.config, {'CSRF_ENABLED': False, 'RATE_LIMIT_ENABLED': False}), \
             patch('app.services.deepgram_tts.synthesize_deepgram', fake_deepgram):
            response = run.app.test_client().post('/api/synthesize', json={'text': 'Example'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'audio/mpeg')
        self.assertEqual(response.data, b'mock-mp3')

    def test_edge_provider_remains_available(self):
        class FakeEdge:
            def __init__(self, **kwargs):
                self.arguments = kwargs
            async def stream(self):
                yield {'type': 'audio', 'data': b'edge-audio'}
        with patch.dict(os.environ, {'TTS_PROVIDER': 'edge'}), patch('edge_tts.Communicate', FakeEdge):
            self.assertEqual(asyncio.run(tts.synthesize_speech('Hello')), b'edge-audio')


if __name__ == '__main__':
    unittest.main()
