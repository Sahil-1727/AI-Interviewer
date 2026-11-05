"""Start either local LiveKit demo process without using the production DB."""
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def database_identity(url):
    if not url:
        return None
    parsed = urlsplit(url.replace('postgresql+psycopg://', 'postgresql://', 1))
    if parsed.scheme not in {'postgres', 'postgresql'} or not parsed.hostname or not parsed.path.strip('/'):
        return None
    # Neon direct and pooled endpoints can name the same database.
    host = parsed.hostname.replace('-pooler.', '.')
    return host, parsed.path.rstrip('/')


def demo_environment(settings, inherited=None):
    demo = settings.get('demo_db_url')
    production = settings.get('DATABASE_URL')
    identity = database_identity(demo)
    if not identity:
        raise ValueError('Set demo_db_url in .env to a dedicated PostgreSQL database.')
    if production and (not database_identity(production) or identity == database_identity(production)):
        raise ValueError('The demo database must be different from DATABASE_URL.')
    required = ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET',
                'LIVEKIT_AGENT_SHARED_SECRET', 'GEMINI_API_KEY')
    missing = [key for key in required if not settings.get(key, '').strip()]
    if missing:
        raise ValueError('Missing settings: ' + ', '.join(missing))
    if len(settings['LIVEKIT_AGENT_SHARED_SECRET']) < 32:
        raise ValueError('LIVEKIT_AGENT_SHARED_SECRET must be at least 32 characters.')
    env = dict(inherited or {})
    env.update({key: value for key, value in settings.items() if value is not None})
    env.update(DATABASE_URL=demo, APP_ENV='testing', SESSION_COOKIE_SECURE='0',
               LIVEKIT_ENABLED='1', STT_PROVIDER='whisper', TTS_PROVIDER='edge',
               LIVEKIT_AGENT_MODE='conversational', FLASK_DEBUG='0',
               INTERVIEW_BACKEND_URL='http://127.0.0.1:5000')
    # The agent and browser do not need the production Flask session secret.
    env['SECRET_KEY'] = secrets.token_urlsafe(48)
    return env


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {'flask', 'agent'}:
        raise SystemExit('Usage: python scripts/livekit_demo.py {flask|agent}')
    try:
        env = demo_environment(dotenv_values(ROOT / '.env'), os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    os.chdir(ROOT)
    args = [sys.executable, 'run.py'] if sys.argv[1] == 'flask' else [sys.executable, '-m', 'livekit_agent.interview_agent', 'dev']
    print('Starting local LiveKit demo using demo_db_url.', flush=True)
    os.execve(sys.executable, args, env)


if __name__ == '__main__':
    main()
