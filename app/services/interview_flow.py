"""Shared progression rules for HTML forms and the voice conversation API."""
from copy import deepcopy


FIELDS = ("candidate_name", "interview_plan", "current_question", "interview_history", "current_follow_up", "final_scorecard", "evaluation_context")


def question_for(state):
    questions = state.get("interview_plan", {}).get("questions", [])
    index = state.get("current_question", 0)
    if index >= len(questions):
        return None
    return state.get("current_follow_up") or questions[index]


def advance(state, answer, evaluator, unanswered=False):
    """Mutate only after evaluation succeeds. Follow-ups retain existing behavior:
    their answers are evidence on the original history entry for the scorecard.
    """
    if not isinstance(answer, str) or (not answer.strip() and not unanswered):
        raise ValueError("An answer is required.")
    question = question_for(state)
    if question is None:
        raise ValueError("The interview is already complete.")
    answer = answer.strip()
    history = deepcopy(state.get("interview_history", []))
    index = state.get("current_question", 0)
    if unanswered:
        if state.get("current_follow_up"):
            if not history or len(history) != index + 1:
                raise ValueError("The follow-up has no matching answer.")
            history[-1]["follow_up"] = {"question": question["question"], "answer": "", "status": "Unanswered"}
            state.pop("current_follow_up", None)
        else:
            history.append({"question": question["question"], "skill": question.get("skill", "General Knowledge"),
                            "type": question["type"], "answer": "", "status": "Unanswered"})
        state["interview_history"] = history
        state["current_question"] = index + 1
        return question_for(state)
    if state.get("current_follow_up"):
        if not history or len(history) != index + 1:
            raise ValueError("The follow-up has no matching answer.")
        history[-1]["follow_up"] = {"question": question["question"], "answer": answer}
        state["interview_history"] = history
        state.pop("current_follow_up", None)
        state["current_question"] = index + 1
    else:
        evaluation = evaluator({**question, "evaluation_context": state.get("evaluation_context", {})}, answer)
        if not isinstance(evaluation, dict) or not isinstance(evaluation.get("follow_up_needed"), bool):
            raise ValueError("The evaluator returned an invalid response.")
        if evaluation["follow_up_needed"] and not str(evaluation.get("follow_up_question", "")).strip():
            raise ValueError("The evaluator returned an empty follow-up.")
        history.append({"question": question["question"], "skill": question.get("skill", "General Knowledge"),
                        "type": question["type"], "answer": answer, "evaluation": evaluation})
        state["interview_history"] = history
        if evaluation["follow_up_needed"]:
            state["current_follow_up"] = {"question": evaluation["follow_up_question"], "skill": question.get("skill", "General Knowledge"), "type": "follow_up"}
        else:
            state["current_question"] = index + 1
    return question_for(state)


def copy_to_session(state, session):
    for field in FIELDS:
        if field in state:
            session[field] = deepcopy(state[field])
        else:
            session.pop(field, None)
