# AI Interviewer

An AI-powered interviewer that takes a Job Description + candidate Resume, extracts required skills, and conducts an adaptive interview with automated evaluation.

## Flow

```
Job Description + Resume → Gemini LLM → Interview Plan → AI asks questions → Candidate answers → Follow-up questions → Scorecard
```

## Tech Stack

- **Backend:** Python + Flask
- **LLM:** Google Gemini API
- **Frontend:** HTML + CSS + JS

## Setup

1. Clone the repo
   ```bash
   git clone https://github.com/your-username/ai-interviewer.git
   cd ai-interviewer
   ```

2. Create and activate virtual environment
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   ```

3. Install dependencies
   ```bash
   pip install -r requirements.txt
   ```

4. Set up environment variables
   ```bash
   cp .env.example .env
   # Add your Gemini API key to .env
   ```

5. Run the app
   ```bash
   python run.py
   ```

6. Open `http://127.0.0.1:5000`

## Project Structure

```
app/
├── routes/       # Flask blueprints
├── services/     # Gemini API calls
├── models/       # Data models
├── prompts/      # LLM prompts
├── templates/    # HTML templates
└── static/       # CSS, JS
run.py            # Entry point
```

## Status

🚧 Work in progress — prototype phase
