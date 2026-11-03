"""Two actual Gunicorn worker processes with a shared PostgreSQL session."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
import socket
import subprocess
import sys
import time
import unittest
from urllib.request import Request, urlopen


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires a dedicated PostgreSQL database')
class GunicornSessionTests(unittest.TestCase):
    def test_concurrent_updates_across_workers(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, PYTHONPATH='tests:.', RATE_LIMIT_ENABLED='0')
        gunicorn = os.path.join(os.path.dirname(sys.executable), 'gunicorn')
        proc = subprocess.Popen([gunicorn, '--workers', '2', '--threads', '1', '--bind', f'127.0.0.1:{port}', 'gunicorn_fixture:app'],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    response = urlopen(base + '/__test/session/init', timeout=1)
                    cookie = response.headers['Set-Cookie'].split(';', 1)[0]
                    break
                except OSError:
                    if proc.poll() is not None:
                        self.fail('Gunicorn failed to start')
                    time.sleep(.1)
            else:
                self.fail('Gunicorn did not start')
            def fetch(path):
                with urlopen(Request(base + path, headers={'Cookie': cookie}), timeout=5) as response:
                    return json.load(response)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(fetch, ['/__test/session/write/first', '/__test/session/write/second']))
            self.assertEqual(len({item['pid'] for item in results}), 2)
            state = fetch('/__test/session/state')
            self.assertEqual((state['first'], state['second']), ('first', 'second'))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
