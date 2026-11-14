"""Atomic fixed-window limits shared by all application workers."""
import hashlib
import time

from sqlalchemy import text


def consume(engine, identity, category, limit, now=None):
    if limit < 1:
        raise ValueError('Rate limit must be positive.')
    now = time.time() if now is None else now
    bucket = int(now) // 60
    client = hashlib.sha256(identity.encode()).hexdigest()
    with engine.begin() as connection:
        count = connection.execute(text('''
            INSERT INTO request_limits (client_hash, category, bucket, count)
            VALUES (:client, :category, :bucket, 1)
            ON CONFLICT (client_hash, category, bucket)
            DO UPDATE SET count = request_limits.count + 1 RETURNING count
        '''), dict(client=client, category=category, bucket=bucket)).scalar_one()
    return count <= limit, 60 - int(now) % 60
