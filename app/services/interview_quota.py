"""PostgreSQL-backed admission limits for restricted LiveKit interviews."""
import os
import json
import logging
from datetime import timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.database import db
from app.models.interview import InterviewUsage

logger = logging.getLogger(__name__)


class QuotaError(Exception):
    pass


def setting(name, default):
    try:
        result = int(os.getenv(name, str(default)))
    except ValueError:
        raise RuntimeError(f'{name} must be a positive integer.') from None
    if result <= 0:
        raise RuntimeError(f'{name} must be a positive integer.')
    return result


def locked_session():
    if db.engine.dialect.name != 'postgresql':
        raise RuntimeError('Restricted LiveKit usage requires PostgreSQL.')
    return Session(db.engine)


def _lock(session):
    # One transaction-level lock serializes admission decisions across workers.
    session.execute(text('SELECT pg_advisory_xact_lock(725910461)'))
    return session.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)


def reserve(interview_id, account_name):
    if not account_name:
        raise QuotaError('Sign in to start an interview.')
    duration = setting('INTERVIEW_MAX_DURATION_SECONDS', 900)
    user_limit = setting('INTERVIEW_MAX_PER_USER_PER_DAY', 3)
    global_limit = setting('INTERVIEW_MAX_GLOBAL_PER_DAY', 20)
    global_active = setting('INTERVIEW_MAX_ACTIVE', 5)
    with locked_session() as session, session.begin():
        now = _lock(session)
        session.execute(update(InterviewUsage).where(InterviewUsage.ended_at.is_(None),
                        InterviewUsage.expires_at <= now).values(ended_at=now))
        existing = session.scalar(select(InterviewUsage).where(InterviewUsage.account_name == account_name,
                                   InterviewUsage.ended_at.is_(None)))
        if existing:
            if existing.interview_id == interview_id:
                return existing.expires_at
            raise QuotaError('You already have an active interview.')
        if session.scalar(select(InterviewUsage.id).where(InterviewUsage.interview_id == interview_id).limit(1)):
            raise QuotaError('This interview session has ended. Start a new interview.')
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        count = lambda *filters: session.scalar(select(func.count()).select_from(InterviewUsage).where(*filters))
        if count(InterviewUsage.account_name == account_name, InterviewUsage.started_at >= day) >= user_limit:
            raise QuotaError('Your daily interview limit has been reached.')
        if count(InterviewUsage.started_at >= day) >= global_limit:
            raise QuotaError('The daily interview limit has been reached.')
        if count(InterviewUsage.ended_at.is_(None)) >= global_active:
            raise QuotaError('The interview service is at capacity.')
        expiry = now + timedelta(seconds=duration)
        session.add(InterviewUsage(interview_id=interview_id, account_name=account_name,
                                   started_at=now, expires_at=expiry))
    logger.info('%s', json.dumps({'event': 'livekit_interview_reserved', 'duration_seconds': duration}))
    return expiry


def active(interview_id):
    with locked_session() as session, session.begin():
        now = _lock(session)
        record = session.scalar(select(InterviewUsage).where(InterviewUsage.interview_id == interview_id,
                                InterviewUsage.ended_at.is_(None)))
        if not record:
            return False
        if record.expires_at <= now:
            record.ended_at = now
            logger.info('%s', json.dumps({'event': 'livekit_interview_expired'}))
            return False
        return True


def finish(interview_id):
    with locked_session() as session, session.begin():
        now = _lock(session)
        session.execute(update(InterviewUsage).where(InterviewUsage.interview_id == interview_id,
                        InterviewUsage.ended_at.is_(None)).values(ended_at=now))


def cleanup_expired():
    with locked_session() as session, session.begin():
        now = _lock(session)
        return session.execute(update(InterviewUsage).where(InterviewUsage.ended_at.is_(None),
                               InterviewUsage.expires_at <= now).values(ended_at=now)).rowcount
