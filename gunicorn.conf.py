"""Production WSGI defaults; override via environment for the host."""
import os
from dotenv import load_dotenv

load_dotenv()

bind = os.getenv('GUNICORN_BIND', '0.0.0.0:' + os.getenv('PORT', '8000'))
workers = int(os.getenv('GUNICORN_WORKERS', '1'))
threads = int(os.getenv('GUNICORN_THREADS', '4'))
worker_class = 'gthread'
provider_concurrency = int(os.getenv('HTTP_PROVIDER_CONCURRENCY', '2'))
if workers < 1 or threads < 2 or not 0 < provider_concurrency < threads:
    raise RuntimeError('Use positive workers and reserve an HTTP thread beyond HTTP_PROVIDER_CONCURRENCY.')
keepalive = int(os.getenv('GUNICORN_KEEPALIVE_SECONDS', '5'))
# Recycling is opt-in: active WebSockets must drain before replacing workers.
max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', '0'))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', '0'))
if os.getenv('STT_PROVIDER', 'whisper').strip().lower() == 'deepgram' and threads <= int(os.getenv('STREAM_MAX_CONNECTIONS_PER_WORKER', '2')):
    raise RuntimeError('Gunicorn must reserve at least one thread per worker for HTTP requests.')
timeout = int(os.getenv('GUNICORN_TIMEOUT_SECONDS', '180'))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT_SECONDS', '180'))
accesslog = '-'
errorlog = '-'
# Keep request paths and query strings out of access logs (they may contain data).
access_log_format = '%(h)s %(m)s %(s)s %(B)s %(D)s'
preload_app = False
