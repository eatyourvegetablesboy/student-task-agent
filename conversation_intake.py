import json
import os
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "prompts" / "conversation_intake.md"
DEFAULT_MODEL = "gpt-4o-mini"

VALID_CONFIDENCES = {"high", "medium", "low"}
VALID_LEVELS = {"low", "medium", "high"}
VALID_COMMITMENT_TYPES = {
    "gym",
    "class",
    "commute",
    "meal",
    "work",
    "social",
    "errand",
    "personal",
    "other",
}
VALID_TASK_STATUSES = {"suggested", "confirmed", "ignored", "in_progress", "done"}
VALID_MEMORY_TYPES = {
    "preference",
    "pattern",
    "weakness",
    "strength",
    "rule",
    "goal",
    "course_context",
    "time_estimation",
    "avoidance",
    "management_style",
    "other",
}


class ConversationIntakeConfigError(RuntimeError):
    """Raised when Conversation Intake cannot be configured safely."""


class ConversationIntakeResponseError(RuntimeError):
    """Raised when Conversation Intake returns output the app cannot parse."""

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
        raise ConversationIntakeConfigError(
            "OPENAI_API_KEY is missing. Add it to your .env file to use Command Center intake."
        )

    try:
        from openai import OpenAI
    except ImportError as error:
        raise ConversationIntakeConfigError(
            "The openai package is missing. Run: pip install -r requirements.txt"
        ) from error

    return OpenAI(api_key=api_key)


def _read_prompt():
    if not PROMPT_PATH.exists():
        raise ConversationIntakeConfigError("Conversation Intake prompt file is missing.")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _clean_text(value, max_chars=700):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _clean_int(value, minimum=None, maximum=None):
    if value in (None, ""):
        return None

    try:
        number = int(value)
    except (TypeError, ValueError):
        return None

    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _clean_date(value, default_date=None):
    if value in (None, ""):
        return default_date
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        return default_date
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return default_date


def _clean_time(value):
    text = _clean_text(value, 20)
    if not text:
        return None
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError:
        return None
    return text


def _choice(value, valid_values, default=None):
    text = _clean_text(value, 80)
    if not text:
        return default
    text = text.lower()
    return text if text in valid_values else default


def _normalize_morning_updates(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "available_study_minutes": _clean_int(
            raw.get("available_study_minutes"),
            minimum=0,
        ),
        "available_time_blocks": _clean_text(raw.get("available_time_blocks")),
        "fixed_commitments": _clean_text(raw.get("fixed_commitments")),
        "extra_commitments": _clean_text(raw.get("extra_commitments")),
        "sleep_quality": _choice(raw.get("sleep_quality"), VALID_LEVELS),
        "energy_level": _choice(raw.get("energy_level"), VALID_LEVELS),
        "stress_level": _choice(raw.get("stress_level"), VALID_LEVELS),
        "mood": _clean_text(raw.get("mood"), 180),
        "top_personal_priority": _clean_text(raw.get("top_personal_priority"), 260),
        "avoiding_task": _clean_text(raw.get("avoiding_task"), 260),
        "hard_stop_time": _clean_time(raw.get("hard_stop_time")),
        "notes": _clean_text(raw.get("notes")),
    }


def _normalize_commitment(raw, current_date):
    if not isinstance(raw, dict):
        return None

    title = _clean_text(raw.get("title"), 180)
    if not title:
        return None

    return {
        "title": title,
        "commitment_type": _choice(
            raw.get("commitment_type"),
            VALID_COMMITMENT_TYPES,
            default="other",
        ),
        "planned_date": _clean_date(raw.get("planned_date"), default_date=current_date),
        "start_time": _clean_time(raw.get("start_time")),
        "estimated_minutes": _clean_int(raw.get("estimated_minutes"), minimum=1),
        "priority": _clean_int(raw.get("priority"), 1, 5) or 3,
        "notes": _clean_text(raw.get("notes")),
    }


def _normalize_daily_review(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "completed_summary": _clean_text(raw.get("completed_summary")),
        "missed_tasks": _clean_text(raw.get("missed_tasks")),
        "blockers": _clean_text(raw.get("blockers")),
        "avoidance_notes": _clean_text(raw.get("avoidance_notes")),
        "tomorrow_top_priority": _clean_text(raw.get("tomorrow_top_priority"), 260),
        "mood_energy": _choice(raw.get("mood_energy"), VALID_LEVELS),
        "focus_rating": _clean_int(raw.get("focus_rating"), 1, 5),
    }


def _normalize_status_suggestion(raw):
    if not isinstance(raw, dict):
        return None

    status = _choice(raw.get("suggested_status"), VALID_TASK_STATUSES)
    title = _clean_text(raw.get("title"), 220)
    if not status or not title:
        return None

    return {
        "task_id": (
            str(raw.get("task_id")) if raw.get("task_id") not in (None, "") else None
        ),
        "title": title,
        "suggested_status": status,
        "reason": _clean_text(raw.get("reason"), 400) or "Suggested from conversation.",
    }


def _normalize_memory_candidate(raw):
    if not isinstance(raw, dict):
        return None

    memory_type = _choice(raw.get("memory_type"), VALID_MEMORY_TYPES)
    memory_key = _clean_text(raw.get("memory_key"), 160)
    memory_value = _clean_text(raw.get("memory_value"), 700)
    if not memory_type or not memory_key or not memory_value:
        return None

    return {
        "memory_type": memory_type,
        "memory_key": memory_key,
        "memory_value": memory_value,
        "confidence": _choice(raw.get("confidence"), VALID_CONFIDENCES, "medium"),
        "source": "command_center",
        "evidence": _clean_text(raw.get("evidence"), 500),
    }


def _normalize_proposal(parsed, current_date):
    if not isinstance(parsed, dict):
        raise ConversationIntakeResponseError(
            "Conversation Intake returned JSON, but not an object."
        )

    commitments = []
    for item in parsed.get("personal_commitments") or []:
        normalized = _normalize_commitment(item, current_date)
        if normalized:
            commitments.append(normalized)

    status_suggestions = []
    for item in parsed.get("task_status_suggestions") or []:
        normalized = _normalize_status_suggestion(item)
        if normalized:
            status_suggestions.append(normalized)

    memory_candidates = []
    for item in parsed.get("memory_candidates") or []:
        normalized = _normalize_memory_candidate(item)
        if normalized:
            memory_candidates.append(normalized)

    questions = parsed.get("clarification_questions") or []
    if not isinstance(questions, list):
        questions = [questions]

    return {
        "summary": _clean_text(parsed.get("summary"), 800) or "Conversation parsed.",
        "morning_checkin_updates": _normalize_morning_updates(
            parsed.get("morning_checkin_updates")
        ),
        "personal_commitments": commitments[:8],
        "daily_review_update": _normalize_daily_review(
            parsed.get("daily_review_update")
        ),
        "task_status_suggestions": status_suggestions[:8],
        "memory_candidates": memory_candidates[:6],
        "clarification_questions": [
            question for question in (_clean_text(item, 240) for item in questions[:4])
            if question
        ],
        "confidence": _choice(parsed.get("confidence"), VALID_CONFIDENCES, "medium"),
    }


def _compact_context(daily_command_context, recent_messages=None):
    recent_messages = recent_messages or []
    return {
        "current_date": daily_command_context.get("current_date"),
        "morning_checkin": daily_command_context.get("morning_checkin"),
        "personal_commitments": daily_command_context.get("personal_commitments", [])[:6],
        "checkin_answers": daily_command_context.get("checkin_answers", [])[:6],
        "today_plan": daily_command_context.get("today_plan", [])[:3],
        "top_active_tasks_by_urgency": daily_command_context.get(
            "top_active_tasks_by_urgency",
            [],
        )[:8],
        "recent_daily_reviews": daily_command_context.get(
            "recent_daily_reviews",
            [],
        )[:3],
        "recent_study_sessions": daily_command_context.get(
            "recent_study_sessions",
            [],
        )[:5],
        "active_agent_memory": daily_command_context.get(
            "active_agent_memory",
            [],
        )[:8],
        "recent_command_center_messages": recent_messages[:5],
    }


def parse_conversation_message(message, daily_command_context, recent_messages=None):
    """
    Convert a natural-language user message into a confirmable local proposal.

    This does not save anything to the database and does not update tasks.
    """
    cleaned_message = _clean_text(message, 4000)
    if not cleaned_message:
        raise ValueError("Message is required.")

    client = _openai_client()
    prompt = _read_prompt()
    model = os.environ.get("CONVERSATION_INTAKE_MODEL") or os.environ.get(
        "QUESTION_COACH_MODEL",
    ) or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    context = _compact_context(daily_command_context, recent_messages)

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    "Parse this user message into a structured proposal.\n\n"
                    f"Current compact context JSON:\n{json.dumps(context, ensure_ascii=False)}\n\n"
                    f"User message:\n{cleaned_message}"
                ),
            },
        ],
    )

    content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise ConversationIntakeResponseError(
            "Conversation Intake returned invalid JSON. Try again.",
            raw_response=content,
        ) from error

    proposal = _normalize_proposal(parsed, context.get("current_date") or date.today().isoformat())
    proposal["_raw_response"] = content
    return proposal
