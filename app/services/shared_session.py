"""Merge independent session changes under a PostgreSQL row lock."""
from copy import deepcopy
from datetime import datetime, timezone

from flask_session.sqlalchemy import SqlAlchemySessionInterface
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


class SessionConflict(Exception):
    pass


def utcnow():
    # Flask-Session's schema stores UTC as a naive timestamp.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SharedSessionInterface(SqlAlchemySessionInterface):
    def _retrieve_session_data(self, store_id):
        model = self.sql_session_model
        with Session(self.client.engine) as db_session, db_session.begin():
            record = db_session.scalar(select(model).where(model.session_id == store_id))
            if record is None:
                return None
            if record.expiry is None or record.expiry <= utcnow():
                db_session.execute(delete(model).where(model.session_id == store_id, model.expiry <= utcnow()))
                return None
            return self.serializer.decode(record.data)

    def _delete_session(self, store_id):
        model = self.sql_session_model
        with Session(self.client.engine) as db_session, db_session.begin():
            db_session.execute(delete(model).where(model.session_id == store_id))

    def save_session(self, app, session, response):
        if getattr(session, '_public_resource', False):
            return
        try:
            if not session and session.modified:
                model = self.sql_session_model
                store_id = self._get_store_id(session.sid)
                with Session(self.client.engine) as db_session, db_session.begin():
                    record = db_session.scalar(select(model).where(model.session_id == store_id).with_for_update())
                    if record is not None:
                        if self.serializer.decode(record.data) != session._initial_data:
                            raise SessionConflict()
                        db_session.delete(record)
            super().save_session(app, session, response)
        except SessionConflict:
            response.status_code = 409
            response.set_data('{"error":"Your session changed in another request. Refresh and retry."}')
            response.content_type = 'application/json'
            response.headers['Cache-Control'] = 'no-store'
        except SQLAlchemyError:
            response.status_code = 503
            response.set_data('{"error":"Service temporarily unavailable."}')
            response.content_type = 'application/json'
            response.headers['Cache-Control'] = 'no-store'

    def open_session(self, app, request):
        # Public assets and readiness never require a candidate session. Avoid
        # remote SQL reads and cookie writes, even when a browser sends a cookie.
        static_prefix = (app.static_url_path or '/static').rstrip('/') + '/'
        if request.path.startswith(static_prefix) or request.path == '/health/ready':
            session = self.session_class(sid=self._generate_sid(self.sid_length), permanent=False)
            session._public_resource = True
            return session
        try:
            session = super().open_session(app, request)
        except SQLAlchemyError:
            session = self.session_class(sid=self._generate_sid(self.sid_length), permanent=self.permanent)
            session._storage_unavailable = True
        session._initial_data = deepcopy(dict(session))
        session._existing_sid = request.cookies.get(app.config['SESSION_COOKIE_NAME']) == session.sid
        return session

    def _upsert_session(self, session_lifetime, session, store_id):
        original = session._initial_data
        desired = dict(session)
        model = self.sql_session_model
        with Session(self.client.engine) as db_session, db_session.begin():
            record = db_session.scalar(select(model).where(model.session_id == store_id).with_for_update())
            if record is None:
                # Never resurrect a session deleted or expired during a request.
                if session._existing_sid:
                    return
                record = model(session_id=store_id, data=self.serializer.encode(desired),
                               expiry=utcnow() + session_lifetime)
                db_session.add(record)
                return
            if record.expiry is None or record.expiry <= utcnow():
                db_session.delete(record)
                return
            current = self.serializer.decode(record.data)
            missing = object()
            for key in original.keys() | desired.keys():
                old, new = original.get(key, missing), desired.get(key, missing)
                if old == new:
                    continue
                # Do not silently discard divergent concurrent changes.
                if current.get(key, missing) != old:
                    if current.get(key, missing) == new:
                        continue
                    raise SessionConflict()
                if new is missing:
                    current.pop(key, None)
                else:
                    current[key] = new
            record.data = self.serializer.encode(current)
            record.expiry = utcnow() + session_lifetime
