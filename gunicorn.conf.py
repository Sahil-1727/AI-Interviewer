"""Production WSGI defaults; override via environment for the host."""
import os
from dotenv import load_dotenv

load_dotenv()

bind = os.getenv('GUNICORN_BIND', '0.0.0.0:8000')
workers = int(os.getenv('GUNICORN_WORKERS', '2'))
threads = int(os.getenv('GUNICORN_THREADS', '4'))
worker_class = 'gthread'
if os.getenv('STT_PROVIDER', 'whisper').strip().lower() == 'deepgram' and threads <= int(os.getenv('STREAM_MAX_CONNECTIONS_PER_WORKER', '2')):
    raise RuntimeError('Gunicorn must reserve at least one thread per worker for HTTP requests.')
timeout = int(os.getenv('GUNICORN_TIMEOUT_SECONDS', '180'))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT_SECONDS', '180'))
accesslog = '-'
errorlog = '-'
# Keep request paths and query strings out of access logs (they may contain data).
access_log_format = '%(h)s %(m)s %(s)s %(B)s %(D)s'
preload_app = False
