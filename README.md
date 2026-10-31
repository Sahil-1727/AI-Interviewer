# AI Interviewer

An AI-powered interviewer that takes a Job Description and candidate Resume, analyzes the required skills and competencies, generates personalized interview questions, and conducts an adaptive interview with automated answer evaluation.

## Flow

```text
Job Description + Resume
            ↓
       Gemini LLM
            ↓
     Interview Plan
            ↓
      Interview
            ↓
     Candidate Answer
            ↓
     Gemini Evaluation
            ↓
       Follow-up?
       ↙        ↘
     Yes         No
      ↓           ↓
 Follow-up    Next Question
      ↓
 Follow-up Answer
      ↓
   Next Question
            ↓
      Final Scorecard
```

## Features

- Job Description + Resume based interview generation
- Required skill extraction
- Competency identification
- Personalized interview questions
- Technical, project, problem-solving and behavioral questions
- Automated answer evaluation
- Answer scoring from 0–10
- Strength and weakness identification
- Adaptive follow-up questions
- Interview history tracking
- AI-generated final scorecard

## Tech Stack

- **Backend:** Python + Flask
- **LLM:** Google Gemini API
- **Frontend:** HTML + CSS + JavaScript
- **Environment:** Python virtual environment + `.env`

## Project Structure

```text
AI Interviewer/
├── app/
│   ├── models/
│   ├── prompts/
│   ├── routes/
│   ├── services/
│   │   └── gemini.py
│   ├── static/
│   └── templates/
│
├── venv/
├── .env
├── requirements.txt
├── README.md
└── run.py
```

## Setup

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd ai-interviewer
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv
```

macOS / Linux:

```bash
source venv/bin/activate
```

Windows:

```bash
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file:

```text
GEMINI_API_KEY=your_api_key_here
```

### 5. Run the application

```bash
python run.py
```

### 6. Open the application

```text
http://127.0.0.1:5000
```

## Status

🚧 **Work in Progress — MVP / Prototype**

### Currently implemented

- Gemini API integration
- Structured interview-plan generation
- Personalized interview questions
- Text-based interview flow
- Candidate answer submission
- Automated answer evaluation
- 0–10 answer scoring
- Adaptive follow-up questions
- Interview history
- Final AI scorecard

### Planned

- Resume PDF upload and parsing
- Persistent database storage
- Speech-to-text
- Text-to-speech
- Real-time voice interview
- Improved interview UI
- Interview analytics
- Deployment