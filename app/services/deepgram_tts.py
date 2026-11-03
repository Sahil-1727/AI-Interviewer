"""Deepgram Aura REST synthesis; credentials never leave the server."""
import os

import httpx

from app.voice_config import voice_config

MAX_AUDIO_BYTES = 8 * 1024 * 1024


async def synthesize_deepgram(text):
    key = os.getenv('DEEPGRAM_API_KEY', '').strip()
    if not key:
        raise RuntimeError('Deepgram speech service is not configured.')
    model = os.getenv('DEEPGRAM_TTS_MODEL', 'aura-2-thalia-en').strip()
    if not model:
        raise RuntimeError('Deepgram voice model is not configured.')
    timeout = httpx.Timeout(voice_config()['TTS_TIMEOUT_SECONDS'], connect=15.0)
    chunks = []
    size = 0
    try:
        # httpx verifies certificates by default; do not follow redirects with credentials.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream('POST', 'https://api.deepgram.com/v1/speak',
                                     params={'model': model, 'encoding': 'mp3'},
                                     headers={'Authorization': 'Token ' + key},
                                     json={'text': text}) as response:
                if response.status_code != 200:
                    raise RuntimeError('Deepgram speech synthesis failed.')
                content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                if content_type not in {'audio/mpeg', 'audio/mp3', 'application/octet-stream'}:
                    raise RuntimeError('Deepgram returned an invalid audio response.')
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise RuntimeError('Deepgram audio response exceeded the size limit.')
                    chunks.append(chunk)
    except httpx.HTTPError:
        # Do not relay provider messages or request headers to callers/logs.
        raise RuntimeError('Deepgram speech service is unavailable.') from None
    audio = b''.join(chunks)
    if not audio:
        raise RuntimeError('Deepgram returned empty audio.')
    return audio
