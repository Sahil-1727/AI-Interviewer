"""Database extension and migration app, kept separate from interview services."""
import os

from flask import Flask
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData

metadata = MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})
db = SQLAlchemy(metadata=metadata, engine_options={"pool_pre_ping": True})
migrate = Migrate()


def init_database(app):
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL must be configured before using the database.")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    app.config["SQLALCHEMY_DATABASE_URI"] = url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    migrate.init_app(app, db)


def migration_app():
    from app.models import interview  # noqa: F401 - register migration metadata
    from flask_session.sqlalchemy.sqlalchemy import create_session_model

    if 'web_sessions' not in db.metadata.tables:
        create_session_model(db, 'web_sessions')

    app = Flask(__name__)
    init_database(app)
    return app
