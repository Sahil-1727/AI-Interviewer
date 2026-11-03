"""Offline schema checks: no external services, model calls, or real interview data."""
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import db, migration_app
from app.models.interview import Answer, ConversationMessage, Evaluation, Interview, Question, Scorecard, User


class DatabaseSchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///" + self.directory.name + "/schema.sqlite"}):
            self.app = migration_app()
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(db.session.remove)
        db.create_all()

    def test_relationships_and_unique_answers(self):
        user = User(display_name="Candidate")
        interview = Interview(owner=user, job_description="Basic Java", plan={"questions": []})
        question = Question(interview=interview, sequence=0, kind="technical", skill="Java", text="Explain a class")
        answer = Answer(question=question, status="answered", transcript="A template for objects")
        db.session.add_all([user, answer])
        db.session.flush()
        evaluation = Evaluation(answer=answer, score=7, strengths=[], weaknesses=[], feedback="Good", follow_up_needed=False)
        db.session.add(evaluation)
        db.session.commit()
        self.assertEqual(db.session.get(Interview, interview.id).owner.display_name, "Candidate")
        self.assertEqual(db.session.get(Evaluation, evaluation.id).answer.transcript, "A template for objects")
        db.session.add(Answer(question_id=question.id, status="unanswered"))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_transcript_order_and_single_scorecard(self):
        user = User(display_name="Candidate")
        interview = Interview(owner=user, job_description="Role", plan={})
        db.session.add(interview)
        db.session.flush()
        db.session.add_all([ConversationMessage(interview_id=interview.id, sequence=1, kind="answer", text="Answer"),
                            ConversationMessage(interview_id=interview.id, sequence=0, kind="question", text="Question"),
                            Scorecard(interview_id=interview.id, result={}, completion_status="completed")])
        db.session.commit()
        messages = db.session.scalars(select(ConversationMessage).where(ConversationMessage.interview_id == interview.id).order_by(ConversationMessage.sequence)).all()
        self.assertEqual([message.kind for message in messages], ["question", "answer"])
        db.session.add(Scorecard(interview_id=interview.id, result={}, completion_status="completed"))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()


if __name__ == "__main__":
    unittest.main()
