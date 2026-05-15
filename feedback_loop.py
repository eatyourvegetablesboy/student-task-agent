import hashlib
import json
from datetime import date, datetime


def _clean_text(value):
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _parse_json(value, default):
    if not value:
        return default

    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _date_text(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _same_day(value, target_date):
    return _date_text(value) == target_date


def _task_key(task):
    task_id = task.get("id")
    return str(task_id) if task_id not in (None, "") else None


def _main_task_key(task):
    task_id = task.get("task_id")
    return str(task_id) if task_id not in (None, "") else None


def _matches_task(session, main_task):
    task_id = _main_task_key(main_task)
    if task_id and str(session.get("task_id")) == task_id:
        return True

    title = (_clean_text(main_task.get("title")) or "").casefold()
    session_title = (_clean_text(session.get("task_title")) or "").casefold()
    return bool(title and session_title and title == session_title)


def _task_completed(main_task, task_lookup, sessions):
    task_id = _main_task_key(main_task)
    task = task_lookup.get(task_id) if task_id else None
    if task and task.get("status") == "done":
        return True

    return any(
        _matches_task(session, main_task)
        and session.get("completion_status") == "completed"
        for session in sessions
    )


def _task_actual_minutes(main_task, sessions):
    total = 0
    for session in sessions:
        if not _matches_task(session, main_task):
            continue
        try:
            total += int(session.get("actual_minutes") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _task_has_focus(main_task, sessions):
    return any(_matches_task(session, main_task) for session in sessions)


def _planning_accuracy(score):
    if score >= 75:
        return "strong"
    if score >= 45:
        return "mixed"
    if score > 0:
        return "weak"
    return "no_data"


def _available_minutes(input_context):
    checkin = input_context.get("morning_checkin") or {}
    try:
        minutes = int(checkin.get("available_study_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    return minutes if minutes > 0 else None


def _estimated_minutes(task):
    try:
        minutes = int(task.get("estimated_minutes") or 0)
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


def _candidate_hash(memory_type, memory_key, memory_value):
    raw = f"{memory_type}|{memory_key}|{memory_value}".lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _memory_candidate(memory_type, memory_key, memory_value, evidence, confidence="medium"):
    return {
        "candidate_hash": _candidate_hash(memory_type, memory_key, memory_value),
        "memory_type": memory_type,
        "memory_key": memory_key,
        "memory_value": memory_value,
        "confidence": confidence,
        "source": "feedback_loop",
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
        "decision_status": "pending",
    }


def evaluate_daily_command(
    command_record,
    tasks,
    study_sessions,
    daily_review=None,
    review_date=None,
):
    """
    Compare one saved Daily Command against actual local execution data.

    This is intentionally rule-based: no AI call, no automatic task changes, and
    no automatic memory writes.
    """
    command_date = command_record["command_date"]
    review_date = review_date or date.today().isoformat()
    command = _parse_json(command_record.get("output_json"), {})
    input_context = _parse_json(command_record.get("input_summary_json"), {})
    main_tasks = command.get("main_tasks") or []
    day_sessions = [
        session for session in study_sessions
        if _same_day(session.get("created_at") or session.get("start_time"), command_date)
    ]
    task_lookup = {
        _task_key(task): task
        for task in tasks
        if _task_key(task) is not None
    }

    completed_tasks = []
    incomplete_tasks = []
    avoidance_flags = []
    time_notes = []

    for main_task in main_tasks:
        title = _clean_text(main_task.get("title")) or "Untitled task"
        completed = _task_completed(main_task, task_lookup, day_sessions)
        actual_minutes = _task_actual_minutes(main_task, day_sessions)
        estimated = _estimated_minutes(main_task)

        if completed:
            completed_tasks.append(title)
        else:
            incomplete_tasks.append(title)
            if not _task_has_focus(main_task, day_sessions):
                avoidance_flags.append(
                    f"No focus session found for recommended task: {title}"
                )

        if estimated and actual_minutes:
            if actual_minutes > estimated * 1.5:
                time_notes.append(
                    f"{title} took longer than estimated: "
                    f"{actual_minutes} min actual vs {estimated} min planned."
                )
            elif actual_minutes < estimated * 0.5 and completed:
                time_notes.append(
                    f"{title} may have been overestimated: "
                    f"{actual_minutes} min actual vs {estimated} min planned."
                )

    if daily_review and daily_review.get("avoidance_notes"):
        avoidance_flags.append(
            "Daily Review included avoidance notes: "
            f"{daily_review['avoidance_notes']}"
        )

    focus_minutes = 0
    for session in day_sessions:
        try:
            focus_minutes += int(session.get("actual_minutes") or 0)
        except (TypeError, ValueError):
            pass

    main_tasks_total = len(main_tasks)
    main_tasks_completed = len(completed_tasks)
    available_minutes = _available_minutes(input_context)
    estimated_total = sum(
        minutes for minutes in (_estimated_minutes(task) for task in main_tasks)
        if minutes is not None
    )

    task_score = (
        (main_tasks_completed / main_tasks_total) * 60
        if main_tasks_total
        else 0
    )
    focus_target = available_minutes or 120
    focus_score = min(25, (focus_minutes / focus_target) * 25) if focus_target else 0
    review_score = 15 if daily_review else 0
    completion_score = round(min(100, task_score + focus_score + review_score), 1)

    overload_warning = None
    if available_minutes and estimated_total > available_minutes * 1.2:
        overload_warning = (
            f"Daily Command estimated {estimated_total} min of main tasks, "
            f"but Morning Check-In had {available_minutes} study minutes."
        )

    if main_tasks_total == 0:
        feedback_summary = "No main tasks were saved in this Daily Command."
    elif main_tasks_completed == main_tasks_total:
        feedback_summary = (
            "All main Daily Command tasks appear completed or covered by "
            "completed focus sessions."
        )
    else:
        feedback_summary = (
            f"Completed {main_tasks_completed} of {main_tasks_total} main tasks. "
            f"Logged {focus_minutes} focus minutes across {len(day_sessions)} sessions."
        )

    memory_candidates = []
    if overload_warning:
        memory_candidates.append(_memory_candidate(
            "time_estimation",
            "daily_plan_overload_risk",
            "Daily plans should stay within available study minutes; overloaded plans reduce execution quality.",
            {
                "command_date": command_date,
                "estimated_total": estimated_total,
                "available_minutes": available_minutes,
            },
        ))

    for flag in avoidance_flags[:3]:
        memory_candidates.append(_memory_candidate(
            "avoidance",
            "possible_task_avoidance",
            flag,
            {"command_date": command_date, "flag": flag},
            confidence="low",
        ))

    for note in time_notes[:3]:
        memory_candidates.append(_memory_candidate(
            "time_estimation",
            "time_estimation_signal",
            note,
            {"command_date": command_date, "note": note},
            confidence="medium",
        ))

    return {
        "review": {
            "command_id": command_record["id"],
            "command_date": command_date,
            "review_date": review_date,
            "completion_score": completion_score,
            "planning_accuracy": _planning_accuracy(completion_score),
            "main_tasks_completed": main_tasks_completed,
            "main_tasks_total": main_tasks_total,
            "focus_minutes": focus_minutes,
            "focus_sessions_count": len(day_sessions),
            "avoidance_flags": json.dumps(avoidance_flags, ensure_ascii=False),
            "time_estimation_notes": "\n".join(time_notes) or None,
            "overload_warning": overload_warning,
            "feedback_summary": feedback_summary,
        },
        "memory_candidates": memory_candidates,
        "details": {
            "completed_tasks": completed_tasks,
            "incomplete_tasks": incomplete_tasks,
            "avoidance_flags": avoidance_flags,
            "time_estimation_notes": time_notes,
            "estimated_total": estimated_total,
            "available_minutes": available_minutes,
        },
    }
