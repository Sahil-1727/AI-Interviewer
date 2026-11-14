"""Read-only account history. Ownership is checked in every database query."""
from flask import Blueprint, abort, g, jsonify, render_template, request
from sqlalchemy import func, select
from sqlalchemy.orm import aliased, load_only

from app.database import db
from app.models.interview import Answer, ConversationMessage, Evaluation, Interview, Question, Scorecard

history_bp = Blueprint('history', __name__)
PAGE_SIZE = 20


def owner_id():
    account_id = getattr(getattr(g, 'current_account', None), 'id', None)
    if not account_id:
        abort(401)
    return account_id


def owned_interview(identity):
    interview = db.session.scalar(select(Interview).options(
        load_only(Interview.id, Interview.owner_id, Interview.plan)).where(
        Interview.id == identity, Interview.owner_id == owner_id()))
    if interview is None:
        abort(404)
    return interview


@history_bp.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@history_bp.get('/my-interviews')
def index():
    page = request.args.get('page', default=1, type=int)
    if page < 1 or page > 10000:
        abort(400)
    account = owner_id()
    rows = db.session.execute(select(Interview, Scorecard.id.label('report_id'))
        .options(load_only(Interview.id, Interview.created_at, Interview.status, Interview.plan))
        .outerjoin(Scorecard, Scorecard.interview_id == Interview.id)
        .where(Interview.owner_id == account)
        .order_by(Interview.created_at.desc(), Interview.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    return render_template('history.html', rows=rows[:PAGE_SIZE], page=page,
                           has_next=len(rows) > PAGE_SIZE)


@history_bp.get('/my-interviews/<identity>/report')
def report(identity):
    interview = owned_interview(identity)
    saved = db.session.scalar(select(Scorecard).where(Scorecard.interview_id == interview.id))
    if saved is None:
        abort(404)
    answered = db.session.scalar(select(func.count(Question.id)).join(Answer).join(Evaluation)
        .where(Question.interview_id == interview.id, Question.parent_id.is_(None)))
    unanswered = [{'question': question, 'status': 'Unanswered'} for question in
        db.session.scalars(select(Question.text).join(Answer)
            .where(Question.interview_id == interview.id, Answer.status == 'unanswered')).all()]
    return render_template('scorecard.html', candidate_name=g.current_account.display_name,
        scorecard=saved.result, early_ended=saved.early_ended,
        answered_questions=answered, unanswered_questions=unanswered,
        planned_questions=len((interview.plan or {}).get('questions', [])),
        historical_interview_id=interview.id)


@history_bp.get('/my-interviews/<identity>/transcript')
def transcript(identity):
    interview = owned_interview(identity)
    messages = db.session.scalars(select(ConversationMessage)
        .where(ConversationMessage.interview_id == interview.id)
        .order_by(ConversationMessage.sequence)).all()
    response = jsonify(interview_id=interview.id, messages=[{
        'sequence': item.sequence, 'kind': item.kind, 'text': item.text,
        'created_at': item.created_at.isoformat(),
    } for item in messages])
    response.headers['Content-Disposition'] = 'attachment; filename="interview-transcript.json"'
    return response


@history_bp.get('/my-interviews/<identity>/review')
def review(identity):
    interview = owned_interview(identity)
    page = request.args.get('page', default=1, type=int)
    if page < 1 or page > 10000:
        abort(400)
    parent = aliased(Question)
    rows = db.session.execute(select(Question, Answer, Evaluation)
        .outerjoin(parent, Question.parent_id == parent.id)
        .outerjoin(Answer, Answer.question_id == Question.id)
        .outerjoin(Evaluation, Evaluation.answer_id == Answer.id)
        .where(Question.interview_id == interview.id)
        .order_by(func.coalesce(parent.sequence, Question.sequence),
                  Question.parent_id.is_not(None), Question.sequence, Question.id)
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    return render_template('review.html', interview=interview, rows=rows[:PAGE_SIZE],
                           page=page, has_next=len(rows) > PAGE_SIZE)
