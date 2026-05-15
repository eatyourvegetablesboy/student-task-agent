import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "prompts" / "question_coach.md"
DEFAULT_MODEL = "gpt-4o-mini"
VALID_ANSWER_TYPES = {"text", "number", "time", "choice"}


class QuestionCoachConfigError(RuntimeError):
    """Raised when Question Coach cannot be configured safely."""


class QuestionCoachResponseError(RuntimeError):
    """Raised when Question Coach returns output the app cannot parse."""

    def __init__(self, message, raw_response=None):
        super().__init__(message)
        self.raw_response = raw_response


def _load_env_file():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def has_openai_api_key():
    _load_env_file()
    return bool(os.environ.get("OPENAI_API_KEY"))


def _openai_client():
    _load_env_file()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise QuestionCoachConfigError(
            "OPENAI_API_KEY is missing. Add it to your .env file to use Question Coach."
        )

    try:
        from openai import OpenAI
    except ImportError as error:
        raise QuestionCoachConfigError(
            "The openai package is missing. Run: pip install -r requirements.txt"
        ) from error

    return OpenAI(api_key=api_key)


def _read_prompt():
    if not PROMPT_PATH.exists():
        raise QuestionCoachConfigError("Question Coach prompt file is missing.")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _truncate(value, max_chars=500):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _compact_context(daily_command_context, existing_answers):
    checkin = daily_command_context.get("morning_checkin") or {}
    missing_fields = [
        key for key in (
            "available_study_minutes",
            "available_time_blocks",
            "fixed_commitments",
            "extra_commitments",
            "energy_level",
            "stress_level",
            "hard_stop_time",
            "avoiding_task",
        )
        if not checkin.get(key)
    ]

    return {
        "current_date": daily_command_context.get("current_date"),
        "missing_morning_checkin_fields": missing_fields,
        "morning_checkin": checkin,
        "existing_answers": [
            {
                "question": _truncate(answer.get("question"), 220),
                "answer": _truncate(answer.get("answer"), 220),
            }
            for answer in existing_answers[:10]
        ],
        "counts": daily_command_context.get("counts", {}),
        "top_active_tasks_by_urgency": daily_command_context.get(
            "top_active_tasks_by_urgency",
            [],
        )[:6],
        "today_plan": daily_command_context.get("today_plan", [])[:3],
        "recent_daily_reviews": daily_command_context.get(
            "recent_daily_reviews",
            [],
        )[:3],
        "active_agent_memory": daily_command_context.get(
            "active_agent_memory",
            [],
        )[:10],
    }


def _normalize_question(question):
    if not isinstance(question, dict):
        return None

    text = _truncate(question.get("question"), 240)
    if not text:
        return None

    answer_type = (_truncate(question.get("answer_type"), 30) or "text").lower()
    if answer_type not in VALID_ANSWER_TYPES:
        answer_type = "text"

    return {
        "question": text,
        "reason": _truncate(question.get("reason"), 300) or "This helps today's plan.",
        "answer_type": answer_type,
    }


def generate_checkin_questions(
    daily_command_context,
    existing_answers=None,
    max_questions=3,
):
    """
    Ask a cheap, compact AI call for the next useful check-in questions.

    This function does not update tasks or generate a Daily Command.
    """
    existing_answers = existing_answers or []
    client = _openai_client()
    prompt = _read_prompt()
    model = os.environ.get("QUESTION_COACH_MODEL") or os.environ.get(
        "OPENAI_MODEL",
        DEFAULT_MODEL,
    )
    compact_context = _compact_context(daily_command_context, existing_answers)
    compact_context["max_questions"] = max(1, min(3, int(max_questions)))

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    "Ask the most useful follow-up questions from this compact "
                    "Daily Command context JSON:\n\n"
                    f"{json.dumps(compact_context, ensure_ascii=False)}"
                ),
            },
        ],
    )

    content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise QuestionCoachResponseError(
            "Question Coach returned invalid JSON. Try generating again.",
            raw_response=content,
        ) from error

    raw_questions = parsed.get("questions", [])
    if not isinstance(raw_questions, list):
        raw_questions = []

    questions = []
    for question in raw_questions:
        normalized = _normalize_question(question)
        if normalized:
            questions.append(normalized)
    return questions[:compact_context["max_questions"]]
