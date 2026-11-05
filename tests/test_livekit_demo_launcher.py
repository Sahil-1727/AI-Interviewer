import unittest
from scripts.livekit_demo import demo_environment


class DemoLauncherTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            'demo_db_url': 'postgresql://user:password@demo.example/db',
            'DATABASE_URL': 'postgresql://user:password@prod.example/db',
            'LIVEKIT_URL': 'wss://example.test', 'LIVEKIT_API_KEY': 'key',
            'LIVEKIT_API_SECRET': 'secret', 'LIVEKIT_AGENT_SHARED_SECRET': 'x' * 32,
            'DEEPGRAM_API_KEY': 'test', 'GEMINI_API_KEY': 'test',
            'APP_ENV': 'production', 'LIVEKIT_ENABLED': '0',
        }

    def test_demo_overrides_production_configuration(self):
        env = demo_environment(self.settings, {'DATABASE_URL': 'old'})
        self.assertEqual(env['DATABASE_URL'], self.settings['demo_db_url'])
        self.assertEqual(env['APP_ENV'], 'testing')
        self.assertEqual(env['LIVEKIT_ENABLED'], '1')
        self.assertEqual(env['SESSION_COOKIE_SECURE'], '0')

    def test_rejects_same_direct_and_pooled_neon_database(self):
        self.settings['DATABASE_URL'] = 'postgresql://user:password@demo-pooler.example/db'
        with self.assertRaisesRegex(ValueError, 'different'):
            demo_environment(self.settings)

    def test_missing_credentials_fail_without_printing_values(self):
        self.settings['LIVEKIT_API_KEY'] = ''
        with self.assertRaisesRegex(ValueError, 'LIVEKIT_API_KEY'):
            demo_environment(self.settings)

    def test_worker_launcher_uses_package_entrypoint_without_secret_output(self):
        from pathlib import Path
        source = (Path(__file__).parents[1] / 'scripts' / 'livekit_demo.py').read_text()
        self.assertIn("'-m', 'livekit_agent.interview_agent', 'dev'", source)
