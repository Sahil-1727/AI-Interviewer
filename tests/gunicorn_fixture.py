"""Test-only WSGI fixture, never imported by the application."""
import os
import time
from flask import jsonify, session
from run import app


@app.get('/__test/session/init')
def initialize_test_session():
    session['test_base'] = 'ready'
    return jsonify(pid=os.getpid())


@app.get('/__test/session/write/<key>')
def write_test_session(key):
    if key not in {'first', 'second'}:
        return '', 404
    session[key] = key
    time.sleep(1)
    return jsonify(pid=os.getpid())


@app.get('/__test/session/state')
def test_session_state():
    return jsonify(first=session.get('first'), second=session.get('second'), pid=os.getpid())
