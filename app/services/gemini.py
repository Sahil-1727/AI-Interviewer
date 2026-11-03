import os
import json
from functools import lru_cache
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

@lru_cache(maxsize=1)
def get_client():
    # A timeout/connection failure has an ambiguous external outcome. Disable
    # SDK retries; the conversation journal owns deliberate recovery attempts.
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=types.HttpOptions(
        timeout=int(float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60")) * 1000),
        retry_options=types.HttpRetryOptions(attempts=1),
    ))


def generate_interview_plan(candidate_name, job_description, resume):

    prompt = f"""
You are an expert technical interviewer.

Create a personalized interview plan for this candidate based on
the job description and resume.

Candidate:
{candidate_name}

Job Description:
{job_description}

Resume:
{resume}

Return ONLY valid JSON.

Use exactly this structure:

{{
    "required_skills": [
        "skill 1",
        "skill 2"
    ],
    "competencies": [
        "competency 1",
        "competency 2"
    ],
    "questions": [
        {{
            "type": "technical",
            "skill": "skill being evaluated",
            "question": "question text"
        }},
        {{
            "type": "project",
            "skill": "skill being evaluated",
            "question": "question text"
        }},
        {{
            "type": "problem_solving",
            "skill": "skill being evaluated",
            "question": "question text"
        }}
    ]
}}

Generate exactly 3 questions. Give the job description priority: ask 2 questions
about the job's stated skills and requirements, and 1 high-level question about
a project or experience on the resume. This approximates a 75% job description,
25% resume mix for a three-question interview. Do not probe deeply into resume
projects or unrelated technologies. The resume question should invite a simple
overview, the candidate's contribution, or a lesson learned.
For job-related questions, use the job description as the authority on scope
and difficulty; a skill listed as basic means foundational concepts, not advanced
internals, expert design, or difficult edge cases. Do not raise the difficulty
because the resume mentions a complex project. When requirements are broad or
experience is limited, favor entry-level questions and plain language.
Do not invent experience that is not present in the resume.
This is a voice-only interview, not a coding assessment. Never ask the candidate
to write code, implement functions, type SQL, or use a code editor. Ask clear,
conversational conceptual, practical, scenario, project, or behavioral questions.
For programming topics ask the candidate to explain their approach verbally.
Match difficulty to the role, candidate experience, and any entry-level or
internship expectations in the job description.
"""


    response = get_client().models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        contents=prompt
    )

    response_text = response.text.strip()

    # Remove markdown code fences if Gemini adds them
    if response_text.startswith("```"):
        response_text = response_text.replace("```json", "")
        response_text = response_text.replace("```", "")
        response_text = response_text.strip()

    interview_plan = json.loads(response_text)
    # Keep test interviews short even if the model returns extra questions.
    interview_plan["questions"] = interview_plan["questions"][:3]

    for question in interview_plan["questions"]:
        question["skill"] = (
            question.get("skill")
            or question.get("competency")
            or question.get("category")
            or "General Knowledge"
        )

    return interview_plan

def evaluate_answer(question, answer):

    prompt = f"""
You are an expert technical interviewer evaluating a candidate's answer.

Question:
{question["question"]}

Skill being evaluated:
{question.get("skill") or question.get("competency") or "General Knowledge"}

Question type:
{question["type"]}

Candidate's answer:
{answer}

Role and candidate experience context:
{json.dumps(question.get("evaluation_context", {}), ensure_ascii=False)}

Evaluate the candidate based only on the question and the skill being evaluated.

Consider:
- Technical correctness
- Relevance
- Understanding
- Completeness
- Clarity

Return ONLY valid JSON.

Use exactly this structure:

{{
    "score": 0,
    "strengths": [
        "strength 1"
    ],
    "weaknesses": [
        "weakness 1"
    ],
    "feedback": "Short constructive feedback",
    "follow_up_needed": false,
    "follow_up_question": ""
}}

Rules:

1. Score from 0 to 10 relative to the target role and experience. Use these
   anchors: 0–2 substantially incorrect or irrelevant; 3–4 partial with major
   gaps; 5–6 adequate core understanding with some gaps; 7–8 good, correct
   understanding; 9–10 excellent, accurate and appropriately complete.
   An unanswered question is recorded separately and is never evaluated here.
   Reward correct core concepts even if concise; do not require advanced detail
   unless the question asks for it. Do not award points for incorrect claims.

2. Set "follow_up_needed" to true if:
   - the answer is incorrect,
   - the answer is significantly incomplete,
   - the answer is vague,
   - or the candidate does not demonstrate sufficient understanding.

3. If "follow_up_needed" is true:
   - Generate ONE concise follow-up question.
   - The follow-up must address the candidate's weakness.
   - Do not repeat the original question.
   - The follow-up must remain related to the same skill.
   - Match the job's stated level; for basic skills, ask a simple clarification
     rather than advanced details or a deep dive into a resume project.

4. If "follow_up_needed" is false:
   - Set "follow_up_question" to an empty string.

5. Judge technical understanding rather than speaking style. Do not heavily
   penalize grammar, accent, hesitation, or minor transcription errors. When
   transcription is genuinely unclear, acknowledge recognition uncertainty
   and use a clarifying follow-up rather than assuming lack of knowledge.
   Use follow-ups to explore incomplete answers rather than automatically
   assigning a very low score. Keep every assessment evidence-based.
"""

    response = get_client().models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        contents=prompt
    )

    response_text = response.text.strip()

    if response_text.startswith("```"):
        response_text = response_text.replace("```json", "")
        response_text = response_text.replace("```", "")
        response_text = response_text.strip()

    return json.loads(response_text)

def generate_final_scorecard(candidate_name, interview_history):

    prompt = f"""
You are a senior interviewer preparing the final evaluation of a candidate.

Candidate:
{candidate_name}

Interview History:
{json.dumps(interview_history, indent=2)}

Analyze the complete interview.

Use the individual question evaluations as evidence.
Do not invent skills, experience, or performance that is not present
in the interview history.

Return ONLY valid JSON.

Use exactly this structure:

{{
    "overall_score": 0,
    "competency_scores": [
        {{
            "competency": "Python",
            "score": 0,
            "summary": "Short summary"
        }}
    ],
    "strengths": [
        "strength 1",
        "strength 2",
        "strength 3"
    ],
    "areas_to_improve": [
        "area 1",
        "area 2",
        "area 3"
    ],
    "communication_assessment": "Short assessment of communication and clarity",
    "technical_assessment": "Short assessment of technical knowledge",
    "recommendation": "Strongly Recommend",
    "final_summary": "Overall interview summary"
}}

Rules:

1. overall_score must be between 0 and 10.

2. Calculate the overall assessment based primarily on the
individual evaluation scores.

3. competency_scores should group related questions into meaningful
competencies such as Python, SQL, Machine Learning, Problem Solving,
Communication, etc., depending on what was actually assessed.

4. Do not create a competency that was not assessed.

5. strengths must be supported by the interview evidence.

6. areas_to_improve must be based on actual weaknesses.

7. recommendation must be exactly one of:
   - "Strongly Recommend"
   - "Recommend"
   - "Consider"
   - "Do Not Recommend"

8. Do not make hiring claims beyond what the interview evidence supports.

9. Keep the final summary concise and professional.
"""

    response = get_client().models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        contents=prompt
    )

    response_text = response.text.strip()

    if response_text.startswith("```"):
        response_text = response_text.replace("```json", "")
        response_text = response_text.replace("```", "")
        response_text = response_text.strip()

    return json.loads(response_text)
