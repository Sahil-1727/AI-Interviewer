"""Prompt contracts with a local fake provider; no Gemini requests."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.gemini import evaluate_answer, generate_interview_plan


class PromptTests(unittest.TestCase):
    def test_voice_question_prompt_and_temporary_three_question_cap(self):
        response = {"questions": [{"question": str(i), "type": "technical"} for i in range(4)]}
        generate = Mock(return_value=SimpleNamespace(text=json.dumps(response)))
        with patch("app.services.gemini.get_client", return_value=SimpleNamespace(models=SimpleNamespace(generate_content=generate))):
            plan = generate_interview_plan("Candidate", "Intern role", "Student project")
        prompt = generate.call_args.kwargs["contents"]
        self.assertIn("voice-only", prompt)
        self.assertIn("type SQL", prompt)
        self.assertIn("explain their approach verbally", prompt)
        self.assertEqual(len(plan["questions"]), 3)

    def test_evaluation_prompt_includes_context_rubric_and_uncertainty(self):
        generate = Mock(return_value=SimpleNamespace(text='{}'))
        question = {"question": "Explain Python", "type": "technical", "evaluation_context": {"job_description": "Intern role"}}
        with patch("app.services.gemini.get_client", return_value=SimpleNamespace(models=SimpleNamespace(generate_content=generate))):
            evaluate_answer(question, "A concise answer")
        prompt = generate.call_args.kwargs["contents"]
        for text in ("Intern role", "0–2", "3–4", "5–6", "7–8", "9–10", "transcription is genuinely unclear", "evidence-based"):
            self.assertIn(text, prompt)


if __name__ == "__main__":
    unittest.main()
