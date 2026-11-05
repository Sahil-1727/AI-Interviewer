import os
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch


class RenderStartupTests(unittest.TestCase):
    def test_deepgram_startup_does_not_import_local_speech_dependencies(self):
        script = '''
import importlib.abc
import sys
class BlockLocalSpeech(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'av', 'numpy', 'faster_whisper', 'edge_tts'}:
            raise ModuleNotFoundError('Local speech dependency imported: ' + fullname)
sys.meta_path.insert(0, BlockLocalSpeech())
import run
assert run.app.test_client().get('/').status_code == 302
'''
        env = os.environ.copy()
        env.update(APP_ENV='testing', DATABASE_URL='', STT_PROVIDER='deepgram',
                   TTS_PROVIDER='deepgram', GEMINI_API_KEY='test-only',
                   DEEPGRAM_API_KEY='test-only', CSRF_ENABLED='0', RATE_LIMIT_ENABLED='0')
        result = subprocess.run([sys.executable, '-c', script], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gunicorn_uses_render_port_and_threaded_worker(self):
        with patch.dict(os.environ, {'PORT': '19001', 'STT_PROVIDER': 'deepgram'}, clear=True), \
             patch('dotenv.load_dotenv', return_value=False):
            config = runpy.run_path('gunicorn.conf.py')
        self.assertEqual(config['bind'], '0.0.0.0:19001')
        self.assertEqual(config['workers'], 1)
        self.assertEqual(config['worker_class'], 'gthread')
        self.assertGreater(config['threads'], 2)
