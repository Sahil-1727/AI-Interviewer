"""Set TEST_DATABASE_URL to a dedicated, already migrated PostgreSQL database."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.interview_repository import InterviewNotFound, append_message, create_interview, export_transcript


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires a dedicated migrated PostgreSQL test database")
class PostgreSQLRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ["TEST_DATABASE_URL"]
        if not url.startswith("postgresql"):
            raise ValueError("TEST_DATABASE_URL must refer to a dedicated PostgreSQL database")
        cls.engine = create_engine(url, pool_pre_ping=True)
        cls.sessions = sessionmaker(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_concurrent_order_and_ownership(self):
        with self.sessions.begin() as session:
            interview_id, token = create_interview(session, "Test", "Java basics", "", {
                "questions": [{"type": "technical", "skill": "Java", "question": "Explain Java"}]})

        def write(number):
            with self.sessions.begin() as session:
                append_message(session, interview_id, token, "answer", str(number))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(20)))
        with self.sessions.begin() as session:
            messages = export_transcript(session, interview_id, token)
            self.assertEqual([m["sequence"] for m in messages], list(range(20)))
            self.assertEqual({m["text"] for m in messages}, {str(n) for n in range(20)})
            with self.assertRaises(InterviewNotFound):
                export_transcript(session, interview_id, interview_id)
