from flask import Blueprint, request, render_template
from app.services.gemini import generate_interview_plan

interview_bp = Blueprint("interview", __name__)


@interview_bp.route("/", methods=["GET", "POST"])
def interview():

    if request.method == "POST":

        candidate_name = request.form.get("candidate_name")
        job_description = request.form.get("job_description")
        resume = request.form.get("resume")

        interview_plan = generate_interview_plan(
            candidate_name,
            job_description,
            resume
        )

        return render_template(
            "index.html",
            interview_plan=interview_plan
        )

    return render_template("index.html")