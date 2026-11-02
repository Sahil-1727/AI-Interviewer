"""Manual Stage 1 voice sandbox. Launch: PYTHONPATH=. venv/bin/python tests/voice_demo.py

Uses real local faster-whisper and edge-tts. All non-voice POSTs and the scorecard
are blocked before view execution, so this sandbox cannot consume Gemini calls.
"""
from flask import redirect, request, session
from run import app


@app.before_request
def voice_demo_guard():
    if (request.method == "POST" and request.path not in {"/api/transcribe", "/api/synthesize"}) or request.path == "/final-scorecard":
        return "Stage 1 sandbox: answer submission and Gemini are disabled. Return to /voice-demo.", 409


@app.route("/voice-demo")
def voice_demo():
    session.clear()
    session.update(
        candidate_name="Voice Test Candidate",
        interview_plan={"questions": [{
            "question": "Describe a project you enjoyed. Take your time to explain your approach and what you learned.",
            "type": "behavioral",
            "skill": "Communication",
        }]},
        current_question=0,
        interview_history=[],
    )
    return redirect("/start-interview")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
