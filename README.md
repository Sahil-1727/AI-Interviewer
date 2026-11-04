# AI Interviewer

**Try the voice interview:** [ai-interviewer-795b.onrender.com](https://ai-interviewer-795b.onrender.com)

> **Looking for the conversational voice version?** See the
> [`feature/free-deployment` branch](https://github.com/Sahil-1727/AI-Interviewer/tree/feature/free-deployment)
> for automatic voice interviews, Deepgram speech services, and PostgreSQL.
> This `main` branch contains the original manual interview experience.

A Flask application for interview practice using a job description and PDF resume.
Google Gemini generates personalized questions, evaluates answers, and produces a
final scorecard.

## Current features

- PDF resume upload and text extraction with `pypdf`; resumes must contain
  extractable text (OCR for scanned PDFs is not implemented).
- Personalized technical, project, problem-solving, and behavioral questions.
- Automated answer evaluation with scores from 0–10 and adaptive follow-ups.
- Typed answers or microphone recordings transcribed with `faster-whisper`.
  Transcripts remain editable before submission; transcription currently uses English.
- On-demand question audio through `edge-tts`, with playback and stop controls.
- Optional camera preview. Video is displayed live in the browser only; the
  application does not record, store, or analyze video.
- Redesigned, responsive setup, interview, and report pages with labeled controls,
  keyboard focus indicators, status messages, and reduced-motion support.
- Final scorecards with overall and competency scores, strengths, areas to improve,
  technical and communication assessments, a recommendation, and a printable report.

## Interview flow

The interview is turn-based:

1. Enter a candidate name and job description, then upload a PDF resume.
2. Gemini creates an interview plan; the prompt requests 10–15 questions.
3. Read or play the current question. Type an answer, or record and stop audio,
   then review and edit the transcript before submitting.
4. Gemini evaluates the submitted answer. If a follow-up is needed, answer it
   before advancing to the next main question. Follow-up answers are saved with
   the original answer for the final assessment.
5. After the last main question and any follow-up, view the final scorecard.

Audio recording and transcription happen per answer. Continuous, real-time voice
conversation is not implemented. Microphone and camera access require browser
permission; both are optional for typed interviews.

## Installation and startup

From the project root, with Python 3 and pip available (macOS/Linux):

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Set the required variable in `.env` using your own Gemini API key:

```dotenv
GEMINI_API_KEY=your_gemini_api_key_here
```

Optional: set `WHISPER_MODEL` to choose the transcription model (default: `base`).
Transcription runs locally on the CPU; the first use may download model files.
Gemini requests and question audio generation require internet access.

Start the application:

```bash
venv/bin/python run.py
```

Open <http://127.0.0.1:5000>. This command runs the Flask development server with
debug mode enabled.

## Session storage

Flask-Session uses CacheLib filesystem storage in `instance/sessions/`, created
at startup and ignored by Git. Interview plans, answers, evaluations, and progress
stay on the server; the browser cookie contains only a session ID, avoiding the
cookie size limit as interview history grows.

After upgrading from cookie-based sessions, restart the application and **start a
new interview**. Existing browser-cookie interview data is not migrated.
This local session storage is not database integration.

## Mocked regression tests

With dependencies installed and the environment configured, run:

```bash
venv/bin/python -m unittest discover -s tests -v
```

`tests/test_interview_flow.py` mocks resume extraction and Gemini operations. It
checks completion with large answers across 10- and 15-question interviews,
follow-ups, cookie size, completed-session redirects, missing follow-ups, and
new-interview state reset. These tests do not make Gemini API calls.

The root-level `test_gemini.py`, `test_stt.py`, and `test_tts.py` are manual service
scripts, separate from this mocked suite; they may contact external services,
load models, or require local audio files.

## Project structure

```text
AI Interviewer/
├── run.py                       # Active Flask app, routes, and session configuration
├── requirements.txt
├── .env.example                 # Environment variable template
├── app/
│   ├── services/
│   │   ├── gemini.py            # Interview planning, evaluation, and scorecards
│   │   ├── resume_parser.py     # PDF text extraction
│   │   ├── stt.py               # Audio transcription
│   │   └── tts.py               # Question audio generation
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/                  # ui.js, voice.js, camera.js
│   ├── templates/               # Shared layout, components, setup, interview, scorecard
│   ├── routes/                  # Earlier blueprint; not registered by run.py
│   ├── models/                  # Placeholder package; no database models
│   └── prompts/                 # Placeholder package; prompts currently in gemini.py
├── tests/test_interview_flow.py # Mocked regression suite
├── test_gemini.py               # Manual service scripts
├── test_stt.py
├── test_tts.py
└── instance/sessions/           # Generated local session data; ignored by Git
```

The local `.env` and `venv/` are also ignored by Git.

## Planned work

- Database integration for persistent interview records (not implemented yet).
- Continuous, real-time voice interviews.
- Interview analytics.
- Production deployment configuration.
