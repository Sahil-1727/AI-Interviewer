"""Opt-in real-service browser fixture: one planned question, <=1 follow-up.

This intentionally uses edge-tts, local Whisper and real Gemini quota. It is NOT
part of unittest discovery. Run only for the controlled real-service check.
"""
import asyncio
from pathlib import Path
import tempfile
import wave

from flask import redirect, session
import numpy as np
import run
from app.services.stt import decode_bounded, get_whisper_model
from app.services.tts import synthesize_speech

app = run.app


@app.route('/real-voice-smoke')
def fixture():
    session.clear()
    session.update(candidate_name='Controlled Voice Test', interview_plan={'questions': [{
        'question': 'How would you test a Flask API, including its error handling?',
        'skill': 'Python', 'type': 'technical',
    }]}, current_question=0, interview_history=[])
    return redirect('/start-interview')


def prepare_audio():
    text = ("I would use pytest and Flask's test client to send requests without a running server. "
            "I would test successful responses, validation errors, authentication failures, and missing resources. "
            "I would mock external services and simulate timeouts. I would check status codes, JSON bodies, "
            "and database rollback behavior. Fixtures would isolate test data. Finally I would add an integration "
            "test for the main user flow and inspect logs to diagnose failures.")
    data = asyncio.run(asyncio.wait_for(synthesize_speech(text), timeout=60))
    with tempfile.NamedTemporaryFile(suffix='.mp3') as temporary:
        temporary.write(data); temporary.flush()
        samples = decode_bounded(temporary.name, 90)
    output = Path('test-results')
    output.mkdir(exist_ok=True)
    path = output / 'real-candidate.wav'
    # Synthetic candidate input is separate from interviewer output. A live mic
    # is not used; Chromium's fake-input device restarts the file per capture.
    samples = np.concatenate([np.zeros(8000, dtype=np.float32), samples, np.zeros(30 * 16000, dtype=np.float32)])
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())
    get_whisper_model()  # Warm exactly one cached local model before the check.
    print('Real candidate input ready: test-results/real-candidate.wav', flush=True)


if __name__ == '__main__':
    prepare_audio()
    app.run(host='127.0.0.1', port=5002, debug=False, threaded=True)
