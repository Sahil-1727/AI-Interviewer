"""Read-only account history. Ownership is checked in every database query."""
from flask import Blueprint, Response, abort, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased, load_only

from app.database import db
from app.models.interview import Answer, ConversationMessage, Evaluation, Interview, Question, QuestionBookmark, Scorecard

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
    query_text = request.args.get('q', '').strip()
    status = request.args.get('status', 'all')
    if len(query_text) > 160 or status not in {'all', 'report', 'pending', 'attention'}:
        abort(400)
    query = (select(Interview, Scorecard.id.label('report_id'))
        .options(load_only(Interview.id, Interview.created_at, Interview.status, Interview.plan))
        .outerjoin(Scorecard, Scorecard.interview_id == Interview.id)
        .where(Interview.owner_id == account))
    if query_text:
        query = query.where(or_(Interview.job_description.icontains(query_text, autoescape=True),
            Interview.questions.any(Question.skill.icontains(query_text, autoescape=True))))
    if status == 'report':
        query = query.where(Scorecard.id.is_not(None))
    elif status == 'pending':
        query = query.where(Scorecard.id.is_(None))
    elif status == 'attention':
        query = query.where(Interview.status == 'error', Scorecard.id.is_(None))
    rows = db.session.execute(query
        .order_by(Interview.created_at.desc(), Interview.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    return render_template('history.html', rows=rows[:PAGE_SIZE], page=page,
                           has_next=len(rows) > PAGE_SIZE, query_text=query_text, status=status)



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
    export_format = request.args.get('format', 'json')
    if export_format not in {'json', 'text'}:
        abort(400)
    messages = db.session.scalars(select(ConversationMessage)
        .where(ConversationMessage.interview_id == interview.id)
        .order_by(ConversationMessage.sequence)).all()
    if export_format == 'text':
        body = 'Saved interview transcript\nTimes shown in UTC.\n\n' + '\n\n'.join(
            f"[{item.created_at.isoformat()}] {message_label(item.kind)}\n{item.text}"
            for item in messages)
        response = Response(body, content_type='text/plain; charset=utf-8')
        response.headers['Content-Disposition'] = 'attachment; filename="interview-transcript.txt"'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    response = jsonify(interview_id=interview.id, messages=[{
        'sequence': item.sequence, 'kind': item.kind, 'text': item.text,
        'created_at': item.created_at.isoformat(),
    } for item in messages])
    response.headers['Content-Disposition'] = 'attachment; filename="interview-transcript.json"'
    return response


@history_bp.get('/my-interviews/<identity>/review')
def review(identity):
    interview = owned_interview(identity)
    focused = request.args.get('question')
    page = request.args.get('page', default=1, type=int)
    if page < 1 or page > 10000:
        abort(400)
    parent = aliased(Question)
    rows = db.session.execute(select(Question, Answer, Evaluation)
        .outerjoin(parent, Question.parent_id == parent.id)
        .outerjoin(Answer, Answer.question_id == Question.id)
        .outerjoin(Evaluation, Evaluation.answer_id == Answer.id)
        .where(Question.interview_id == interview.id)
        .where(Question.id == focused if focused else True)
        .order_by(func.coalesce(parent.sequence, Question.sequence),
                  Question.parent_id.is_not(None), Question.sequence, Question.id)
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    if focused and not rows:
        abort(404)
    bookmarks = set(db.session.scalars(select(QuestionBookmark.question_id).where(
        QuestionBookmark.user_id == owner_id(), QuestionBookmark.question_id.in_([row[0].id for row in rows]))))
    return render_template('review.html', interview=interview, rows=rows[:PAGE_SIZE],
                           page=page, has_next=len(rows) > PAGE_SIZE, bookmarks=bookmarks)


@history_bp.post('/my-interviews/<identity>/questions/<question_id>/bookmark')
def bookmark(identity, question_id):
    interview = owned_interview(identity)
    question = db.session.scalar(select(Question.id).where(
        Question.id == question_id, Question.interview_id == interview.id))
    if question is None:
        abort(404)
    action = request.form.get('action')
    if action not in {'save', 'remove'}:
        abort(400)
    if action == 'save':
        # Explicit desired state makes duplicate clicks safe across workers.
        if db.engine.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.session.execute(insert(QuestionBookmark).values(user_id=owner_id(), question_id=question_id)
            .on_conflict_do_nothing(index_elements=['user_id', 'question_id']))
    else:
        from sqlalchemy import delete
        db.session.execute(delete(QuestionBookmark).where(
            QuestionBookmark.user_id == owner_id(), QuestionBookmark.question_id == question_id))
    db.session.commit()
    if request.form.get('return_to') == 'bookmarks':
        return redirect(url_for('history.bookmarks'), code=303)
    return redirect(url_for('history.review', identity=identity, question=question_id), code=303)


@history_bp.get('/my-interviews/bookmarks')
def bookmarks():
    page = request.args.get('page', default=1, type=int)
    if not 1 <= page <= 10000:
        abort(400)
    rows = db.session.execute(select(Question, QuestionBookmark.created_at)
        .join(QuestionBookmark, QuestionBookmark.question_id == Question.id)
        .join(Interview, Interview.id == Question.interview_id)
        .where(QuestionBookmark.user_id == owner_id(), Interview.owner_id == owner_id())
        .order_by(QuestionBookmark.created_at.desc(), Question.id)
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    return render_template('bookmarks.html', rows=rows[:PAGE_SIZE], page=page, has_next=len(rows) > PAGE_SIZE)


@history_bp.get('/my-interviews/progress')
def progress():
    account = owner_id()
    # Scores are comparable as practice indicators, not standardized assessments.
    base = (select(Interview.id.label('interview_id'), Interview.created_at,
        Question.skill, Evaluation.score).select_from(Interview)
        .join(Scorecard, Scorecard.interview_id == Interview.id)
        .join(Question, Question.interview_id == Interview.id)
        .join(Answer, Answer.question_id == Question.id)
        .join(Evaluation, Evaluation.answer_id == Answer.id)
        .where(Interview.owner_id == account, Answer.status == 'answered').subquery())
    skills = db.session.execute(select(base.c.skill, func.avg(base.c.score).label('average'),
        func.count().label('samples')).group_by(base.c.skill).order_by(func.avg(base.c.score), base.c.skill).limit(20)).all()
    recent = db.session.execute(select(base.c.interview_id, base.c.created_at,
        func.avg(base.c.score).label('average'), func.count().label('samples'))
        .group_by(base.c.interview_id, base.c.created_at)
        .order_by(base.c.created_at.desc(), base.c.interview_id.desc()).limit(20)).all()
    reports = db.session.scalar(select(func.count(Scorecard.id)).join(Interview,
        Interview.id == Scorecard.interview_id).where(Interview.owner_id == account))
    average, samples = db.session.execute(select(func.avg(base.c.score), func.count()).select_from(base)).one()
    return render_template('progress.html', skills=skills, recent=recent, reports=reports,
        average=average, samples=samples)


MESSAGE_LABELS = {
    'question': 'Interviewer', 'follow_up': 'Interviewer · Follow-up',
    'assistant': 'Interviewer', 'user': 'Candidate', 'answered': 'Candidate',
    'unanswered': 'Unanswered', 'skipped': 'Skipped', 'completion': 'Interview completed',
}


def message_label(kind):
    return MESSAGE_LABELS.get(kind, 'Interview event')


@history_bp.get('/my-interviews/<identity>/conversation')
def conversation(identity):
    interview = owned_interview(identity)
    page = request.args.get('page', default=1, type=int)
    query_text = request.args.get('q', '').strip()
    if not 1 <= page <= 10000 or len(query_text) > 160:
        abort(400)
    query = select(ConversationMessage).where(ConversationMessage.interview_id == interview.id)
    if query_text:
        query = query.where(ConversationMessage.text.icontains(query_text, autoescape=True))
    messages = db.session.scalars(query.order_by(ConversationMessage.sequence)
        .offset((page - 1) * 40).limit(41)).all()
    return render_template('transcript.html', interview=interview, messages=messages[:40],
        query_text=query_text, page=page, has_next=len(messages) > 40, message_label=message_label)
