import io
import random
import string
import tempfile
import unittest
import warnings
from contextlib import redirect_stdout
from unittest.mock import patch

from cachelib.file import FileSystemCache

import run


class InterviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        original_cache = run.app.session_interface.cache
        run.app.session_interface.cache = FileSystemCache(
            cache_dir=self.directory.name
        )
        self.addCleanup(
            setattr, run.app.session_interface, "cache", original_cache
        )
        original_testing = run.app.testing
        run.app.testing = True
        self.addCleanup(setattr, run.app, "testing", original_testing)
        self.client = run.app.test_client()
        self.cookie_sizes = []
        # Incompressible text reproduces the growth from real interview data.
        generator = random.Random(42)
        self.answer = "".join(
            generator.choices(string.ascii_letters + string.digits, k=6000)
        )
        self.scorecard = {
            "overall_score": 7,
            "competency_scores": [],
            "strengths": ["Clear examples"],
            "areas_to_improve": ["More detail"],
            "communication_assessment": "Clear communication",
            "technical_assessment": "Good understanding",
            "recommendation": "Recommend",
            "final_summary": "Completed every question.",
        }

    def record_cookie_size(self, response):
        for header in response.headers.getlist("Set-Cookie"):
            if header.startswith("session="):
                self.cookie_sizes.append(len(header.encode("utf-8")))
        return response

    def begin_interview(self, count=10):
        plan = {
            "questions": [
                {
                    "type": "technical",
                    "skill": "Python",
                    "question": f"Question {index + 1}: explain your approach.",
                }
                for index in range(count)
            ]
        }
        with (
            patch.object(run, "extract_resume_text", return_value="Resume"),
            patch.object(run, "generate_interview_plan", return_value=plan),
        ):
            response = self.record_cookie_size(self.client.post("/", data={
                "candidate_name": "Test Candidate",
                "job_description": "Python developer",
                "resume": (io.BytesIO(b"resume"), "resume.pdf"),
            }))
        self.assertEqual(response.location, "/start-interview")

    def complete_interview(self, follow_ups, count=10):
        self.begin_interview(count)
        evaluation = {
            "score": 7,
            "strengths": ["Examples"],
            "weaknesses": ["More detail"],
            "feedback": self.answer,
            "follow_up_needed": follow_ups,
            "follow_up_question": "Can you explain further?" if follow_ups else "",
        }
        with (
            patch.object(run, "evaluate_answer", return_value=evaluation),
            patch.object(
                run, "generate_final_scorecard", return_value=self.scorecard
            ) as generate_scorecard,
            warnings.catch_warnings(record=True) as captured,
            redirect_stdout(io.StringIO()),
        ):
            warnings.simplefilter("always")
            for index in range(count):
                question_page = self.client.get("/start-interview")
                self.assertEqual(question_page.status_code, 200)
                self.assertIn(f"Question {index + 1}:".encode(), question_page.data)
                response = self.record_cookie_size(self.client.post(
                    "/submit-answer", data={"answer": self.answer}
                ))
                if follow_ups:
                    self.assertEqual(response.location, "/follow-up")
                    self.assertEqual(self.client.get("/follow-up").status_code, 200)
                    response = self.record_cookie_size(self.client.post(
                        "/submit-follow-up", data={"answer": self.answer}
                    ))
                destination = (
                    "/final-scorecard" if index == count - 1 else "/start-interview"
                )
                self.assertEqual(response.location, destination)

            result = self.client.get(response.location)
            self.assertEqual(result.status_code, 200)
            self.assertIn(b"Completed every question.", result.data)
            generate_scorecard.assert_called_once()
            candidate, history = generate_scorecard.call_args.args
            self.assertEqual(candidate, "Test Candidate")
            self.assertEqual(len(history), count)
            self.assertTrue(all(item["answer"] == self.answer for item in history))
            if follow_ups:
                self.assertTrue(all(
                    item["follow_up"]["answer"] == self.answer for item in history
                ))
            self.assertFalse(any("cookie is too large" in str(w.message) for w in captured))

        self.assertTrue(self.cookie_sizes)
        self.assertLess(max(self.cookie_sizes), 512)
        with self.client.session_transaction() as state:
            self.assertEqual(state["current_question"], count)
            self.assertNotIn("current_follow_up", state)

    def test_tenth_answer_opens_results_with_large_history(self):
        self.complete_interview(follow_ups=False)

    def test_tenth_follow_up_opens_results_with_large_history(self):
        self.complete_interview(follow_ups=True)

    def test_fifteen_question_interview_opens_results(self):
        self.complete_interview(follow_ups=True, count=15)

    def test_completed_interview_redirects_to_results(self):
        self.begin_interview()
        with self.client.session_transaction() as state:
            state["current_question"] = 10
        self.assertEqual(self.client.get("/start-interview").location, "/final-scorecard")
        self.assertEqual(self.client.post("/submit-answer").location, "/final-scorecard")

    def test_follow_up_without_pending_question_does_not_crash_or_advance(self):
        self.begin_interview()
        response = self.client.post("/submit-follow-up", data={"answer": "Retry"})
        self.assertEqual(response.location, "/start-interview")
        with self.client.session_transaction() as state:
            self.assertEqual(state["current_question"], 0)

    def test_new_interview_clears_previous_follow_up(self):
        with self.client.session_transaction() as state:
            state["current_follow_up"] = {"question": "Old follow-up"}
        self.begin_interview()
        with self.client.session_transaction() as state:
            self.assertNotIn("current_follow_up", state)
            self.assertEqual(state["interview_history"], [])


if __name__ == "__main__":
    unittest.main()
