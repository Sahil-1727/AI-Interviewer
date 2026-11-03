"""Deterministic conversational sandbox; Gemini is replaced in this process only.

Launch: PYTHONPATH=. venv/bin/python tests/voice_demo.py
STT/TTS stay real. Browser tests intercept their HTTP responses separately.
"""
from flask import redirect, request, session
import os
import time
import run
import app.services.conversation as conversation
from app.services.conversation import mock_evaluate, mock_scorecard

app = run.app
app.config['VOICE_MOCK_MODE'] = True
run.evaluate_answer = mock_evaluate
run.generate_final_scorecard = mock_scorecard

delay = float(os.getenv('VOICE_DEMO_EVALUATION_DELAY_SECONDS', '0'))
if delay:
    def delayed_mock_evaluate(question, answer):
        time.sleep(delay)
        return mock_evaluate(question, answer)
    conversation.mock_evaluate = delayed_mock_evaluate


def mock_plan(*args):
    return {'questions': [
        {'question': 'Could you briefly introduce yourself and describe a project you enjoyed?',
         'type': 'behavioral', 'skill': 'Communication', 'mock_follow_up': True},
        {'question': 'How would you test and improve a Flask application?',
         'type': 'technical', 'skill': 'Python', 'mock_follow_up': False},
    ]}


run.generate_interview_plan = mock_plan


if os.getenv('DEEPGRAM_TEST_MOCK') == '1':
    # This module is only used by the local test harness. No external speech or
    # Gemini requests are made in this mode.
    import io
    import json
    import wave
    import websocket
    import app.routes.deepgram_stream as relay
    import app.services.deepgram_tts as tts

    stream_counts = {}

    class MockDeepgram:
        def __init__(self, mode='normal', attempt=1):
            self.events = []
            self.has_audio = False
            self.mode = mode
            self.attempt = attempt
            self.started = None
            self.second_sent = False
        def settimeout(self, timeout):
            pass
        def send_binary(self, data):
            if not self.has_audio:
                self.has_audio = True
                self.started = time.monotonic()
                self.events.append(json.dumps({'type': 'SpeechStarted'}))
                if self.mode not in {'silence', 'noise'}:
                    text = 'I built a Flask project and tested the API.'
                    if self.mode == 'disconnect':
                        text = 'I built a Flask project.' if self.attempt == 1 else 'I tested the API.'
                    if self.mode == 'command_repeat' and self.attempt == 1:
                        text = 'Repeat the question.'
                    if self.mode == 'pauses':
                        text = 'First part.'
                    if self.mode == 'candidate_a':
                        text = 'Candidate alpha answer.'
                    if self.mode == 'candidate_b':
                        text = 'Candidate beta answer.'
                    self.events.append(json.dumps({'type': 'Results', 'start': 0, 'is_final': True,
                        'speech_final': self.mode == 'pauses',
                        'channel': {'alternatives': [{'transcript': text, 'confidence': 0.99}]}}))
                    if self.mode == 'pauses':
                        self.events.append(json.dumps({'type': 'UtteranceEnd'}))
                    if self.mode == 'command_done' and self.attempt == 1:
                        self.events.append(json.dumps({'type': 'Results', 'start': 1, 'is_final': True,
                            'channel': {'alternatives': [{'transcript': "I'm done.", 'confidence': 0.99}]}}))
        def send(self, data):
            if json.loads(data).get('type') == 'CloseStream':
                self.events.append(json.dumps({'type': 'Metadata'}))
        def recv(self):
            if self.events:
                return self.events.pop(0)
            if self.mode == 'pauses' and self.started and not self.second_sent and time.monotonic() - self.started >= .8:
                self.second_sent = True
                return json.dumps({'type': 'Results', 'start': 1, 'is_final': True,
                    'channel': {'alternatives': [{'transcript': 'Second part.', 'confidence': 0.99}]}})
            if self.mode == 'disconnect' and self.attempt == 1 and self.has_audio:
                raise websocket.WebSocketConnectionClosedException()
            raise websocket.WebSocketTimeoutException()
        def close(self):
            pass
    def mocked_connection(*args, **kwargs):
        identity = session.get('voice_interview_id')
        stream_counts[identity] = stream_counts.get(identity, 0) + 1
        return MockDeepgram(session.get('_test_deepgram_mode', 'normal'), stream_counts[identity])
    relay.websocket.create_connection = mocked_connection

    async def mock_aura(text):
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\x00\x00' * 2400)
        return output.getvalue()
    tts.synthesize_deepgram = mock_aura


@app.route('/voice-demo')
def voice_demo():
    session.clear()
    session.update(candidate_name='Voice Test Candidate', interview_plan=mock_plan(),
                   current_question=0, interview_history=[])
    if os.getenv('DEEPGRAM_TEST_MOCK') == '1':
        session['_test_deepgram_mode'] = request.args.get('scenario', 'normal')
    return redirect('/start-interview')


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, debug=False, threaded=True)
