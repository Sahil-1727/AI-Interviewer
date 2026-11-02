"""Deterministic conversational sandbox; Gemini is replaced in this process only.

Launch: PYTHONPATH=. venv/bin/python tests/voice_demo.py
STT/TTS stay real. Browser tests intercept their HTTP responses separately.
"""
from flask import redirect, session
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


@app.route('/voice-demo')
def voice_demo():
    session.clear()
    session.update(candidate_name='Voice Test Candidate', interview_plan=mock_plan(),
                   current_question=0, interview_history=[])
    return redirect('/start-interview')


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, debug=False, threaded=True)
