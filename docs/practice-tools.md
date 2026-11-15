# Private practice tools

On `feature/free-deployment`, open **My interviews** to find **Your progress**
and **Bookmarked questions**. Open **Review answers** on any saved interview to
bookmark a question and read its coaching section.

## Bookmarks

Bookmarks are stored in PostgreSQL and scoped to the signed-in account. Saving
or removing uses an explicit desired state, a CSRF-protected POST, and a composite
primary key so concurrent saves do not duplicate records. The server verifies
both interview ownership and question membership. Bookmark links open the exact
question, including questions beyond the first review page. Removing a bookmark
does not remove the question, answer, evaluation, or interview.

## Coaching

Coaching reuses the saved evaluation's strengths and weaknesses. It provides a
clearly labeled, general answer structure for technical or behavioral/project
questions. It does not generate an ideal answer, fabricate technical feedback,
change a score, or call a model. Questions without evaluations retain their
existing unavailable-feedback message.

## Progress

The dashboard averages saved evaluation scores from answered questions in owned
interviews with final scorecards, including early-ended interviews. Follow-ups
count as separate evaluated answers. Unanswered, skipped, and unevaluated answers
are excluded. Counts accompany scores; an empty dataset displays no numerical
average. Skill summaries show up to 20 lowest-scoring skill labels and the
recent list shows up to 20 interviews. Existing scorecard calculations are
unchanged. Difficulty and topic differences mean these are practice indicators,
not standardized ability comparisons.

## Release procedure

This feature requires additive migration `c2a8d6e39104`. Test a database backup
and apply migrations through the normal release procedure before routing traffic
to the new application version:

```sh
flask --app app.database:migration_app db current
flask --app app.database:migration_app db upgrade
flask --app app.database:migration_app db current
```

Use the intended database environment when running these commands. Do not point
local tests at production. The migration only creates `question_bookmarks`; it
does not rewrite existing interview data. The previous app version can keep
running with the extra table. Readiness checks now include the bookmark table.
Do not run a production downgrade: it deletes saved bookmarks.

## Manual checks

1. Sign in, open My interviews, then Review answers.
2. Save a question twice; the bookmark collection should show one record.
3. Open the bookmark; verify the exact saved answer, evaluation, and coaching.
4. Remove the bookmark and confirm the interview data is retained.
5. Check Your progress with no reports, one report, and an early-ended report.
6. Verify unanswered questions do not contribute zeroes or increase sample counts.
7. Sign in as another candidate; neither bookmarks nor progress should be shared.
8. Check the pages on a phone and desktop, including keyboard navigation.

Automated checks use mocked external providers and isolated databases. No live
provider credits or production records are required.
