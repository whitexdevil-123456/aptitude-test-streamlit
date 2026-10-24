from __future__ import annotations

import csv
import html
import io
import math
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh
from supabase import Client, create_client
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()
st.set_page_config(
    page_title="Class Test Portal",
    page_icon="📝",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container {max-width: 1180px; padding-top: 1.6rem; padding-bottom: 3rem;}
      [data-testid="stMetric"] {background: rgba(120,120,120,.07); padding: 14px 16px; border-radius: 12px;}
      .hero {padding: 1.2rem 1.4rem; border: 1px solid rgba(120,120,120,.22); border-radius: 16px; margin-bottom: 1rem;}
      .muted {color: #777; font-size: .92rem;}
      div[data-testid="stForm"] {border-radius: 12px;}
    </style>
    """,
    unsafe_allow_html=True,
)

CATEGORY_LABELS = {"aptitude": "Aptitude Test", "programming": "Programming Test"}
TYPE_LABELS = {"mcq": "MCQ", "short": "Short answer", "coding": "Programming/code"}


def request_student_navigation(page: str) -> None:
    """Request a navigation change for the next rerun. The radio key is set before its widget exists."""
    st.session_state["page"] = page
    st.session_state["student_navigation_target"] = page


def secret(name: str, default: str = "") -> str:
    """Read a setting from Streamlit secrets first, then environment variables."""
    value = os.getenv(name, default)
    try:
        if name in st.secrets:
            value = str(st.secrets[name])
    except Exception:
        pass
    return str(value or default).strip()


def supabase_configured() -> bool:
    return bool(secret("SUPABASE_URL") and secret("SUPABASE_SERVICE_ROLE_KEY"))


def normalize_supabase_url(raw_url: str) -> str:
    """Accept a project root URL; tolerate the commonly copied /rest/v1 suffix."""
    url = (raw_url or "").strip().rstrip("/")
    if url.lower().endswith("/rest/v1"):
        url = url[:-len("/rest/v1")].rstrip("/")

    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise RuntimeError(
            "SUPABASE_URL is invalid. Copy the Project URL from Supabase. "
            "It should look like https://abcdefgh.supabase.co, not a dashboard URL."
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise RuntimeError(
            "SUPABASE_URL must be only the project root URL, for example "
            "https://abcdefgh.supabase.co. Remove any extra path such as "
            "/dashboard, /project, or query string. The app adds /rest/v1 automatically."
        )
    return f"{parsed.scheme}://{parsed.netloc}"


@st.cache_resource(show_spinner=False)
def get_db() -> Client:
    raw_url = secret("SUPABASE_URL")
    key = secret("SUPABASE_SERVICE_ROLE_KEY")
    if not raw_url or not key:
        raise RuntimeError(
            "Supabase is not configured. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY "
            "to .streamlit/secrets.toml or environment variables."
        )
    url = normalize_supabase_url(raw_url)
    return create_client(url, key)


def rows(response: Any) -> list[dict[str, Any]]:
    return list(getattr(response, "data", None) or [])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_answer(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def clean_output(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return "\n".join(line.rstrip() for line in text.split("\n"))


def check_expected(user_answer: str, correct_answer: str) -> bool:
    options = [clean_answer(x) for x in str(correct_answer or "").split("|") if x.strip()]
    return bool(options) and clean_answer(user_answer) in options


def mark_attempt_submitted(attempt_id: str, status: str = "submitted") -> None:
    """Score all persisted answers and close an attempt. Coding can be auto-scored by output."""
    db = get_db()
    attempt_data = rows(db.table("attempts").select("*").eq("id", attempt_id).limit(1).execute())
    if not attempt_data or attempt_data[0].get("status") != "in_progress":
        return

    attempt = attempt_data[0]
    aq_rows = rows(
        db.table("attempt_questions")
        .select("question_id,marks")
        .eq("attempt_id", attempt_id)
        .order("display_order")
        .execute()
    )
    q_ids = [item["question_id"] for item in aq_rows]
    q_by_id: dict[int, dict[str, Any]] = {}
    if q_ids:
        q_by_id = {int(q["id"]): q for q in rows(db.table("questions").select("*").in_("id", q_ids).execute())}
    a_by_id = {
        int(a["question_id"]): a
        for a in rows(db.table("answers").select("*").eq("attempt_id", attempt_id).execute())
    }

    total = 0.0
    maximum = 0.0
    for item in aq_rows:
        qid = int(item["question_id"])
        q = q_by_id.get(qid, {})
        answer = a_by_id.get(qid, {})
        marks = float(item.get("marks") or 0)
        maximum += marks
        awarded = float(answer.get("awarded_marks") or 0)
        is_correct: bool | None = answer.get("is_correct")

        if q.get("question_type") in ("mcq", "short"):
            is_correct = check_expected(answer.get("answer_text", ""), q.get("correct_answer", ""))
            awarded = marks if is_correct else 0.0
        elif q.get("question_type") == "coding":
            expected = q.get("expected_output")
            if expected is not None and str(expected).strip():
                is_correct = clean_output(answer.get("output_answer", "")) == clean_output(expected)
                awarded = marks if is_correct else 0.0
            else:
                # A coding answer without an expected output is left for teacher review.
                is_correct = None
                awarded = float(answer.get("awarded_marks") or 0)

        if answer:
            db.table("answers").update(
                {"is_correct": is_correct, "awarded_marks": awarded}
            ).eq("attempt_id", attempt_id).eq("question_id", qid).execute()
        total += awarded

    db.table("attempts").update(
        {
            "status": status,
            "submitted_at": utc_now().isoformat(),
            "score": round(total, 2),
            "max_score": round(maximum, 2),
        }
    ).eq("id", attempt_id).execute()


def save_answer(attempt_id: str, question_id: int, kind: str) -> None:
    """Widget callback: persist the current response, so a rerun/browser refresh loses less work."""
    try:
        db = get_db()
        suffix = f"{attempt_id}_{question_id}"
        if kind == "code":
            payload = {
                "attempt_id": attempt_id,
                "question_id": question_id,
                "answer_text": None,
                "code_answer": str(st.session_state.get(f"code_{suffix}", "") or ""),
                "output_answer": str(st.session_state.get(f"output_{suffix}", "") or ""),
            }
        else:
            value = st.session_state.get(f"answer_{suffix}", "")
            payload = {
                "attempt_id": attempt_id,
                "question_id": question_id,
                "answer_text": str(value or "") if value is not None else None,
                "code_answer": None,
                "output_answer": None,
            }
        db.table("answers").upsert(payload, on_conflict="attempt_id,question_id").execute()
        st.session_state.pop("answer_save_error", None)
    except Exception as exc:  # avoid losing the whole page due to a temporary network failure
        st.session_state["answer_save_error"] = str(exc)


def persist_visible_answers(attempt_id: str, questions: list[dict[str, Any]]) -> None:
    for q in questions:
        qid = int(q["id"])
        kind = "code" if q.get("question_type") == "coding" else "text"
        suffix = f"{attempt_id}_{qid}"
        keys = (f"code_{suffix}", f"output_{suffix}") if kind == "code" else (f"answer_{suffix}",)
        if any(key in st.session_state for key in keys):
            save_answer(attempt_id, qid, kind)


def get_attempt_questions(attempt_id: str) -> list[dict[str, Any]]:
    db = get_db()
    assignments = rows(
        db.table("attempt_questions")
        .select("question_id,display_order,marks")
        .eq("attempt_id", attempt_id)
        .order("display_order")
        .execute()
    )
    ids = [int(a["question_id"]) for a in assignments]
    if not ids:
        return []
    questions = rows(db.table("questions").select("*").in_("id", ids).execute())
    q_map = {int(q["id"]): q for q in questions}
    answers = rows(db.table("answers").select("*").eq("attempt_id", attempt_id).execute())
    a_map = {int(a["question_id"]): a for a in answers}
    result = []
    for assignment in assignments:
        q = q_map.get(int(assignment["question_id"]))
        if q:
            q = dict(q)
            q["attempt_marks"] = float(assignment.get("marks") or 0)
            q["saved_answer"] = a_map.get(int(q["id"]), {})
            result.append(q)
    return result


def find_active_attempt(student_id: str, category: str) -> dict[str, Any] | None:
    found = rows(
        get_db().table("attempts").select("*")
        .eq("student_id", student_id).eq("category", category).eq("status", "in_progress")
        .order("started_at", desc=True).limit(1).execute()
    )
    return found[0] if found else None


def start_attempt(student: dict[str, Any], category: str) -> tuple[dict[str, Any] | None, str | None]:
    db = get_db()
    existing = find_active_attempt(student["id"], category)
    if existing:
        deadline = parse_dt(existing.get("deadline_at"))
        if deadline and utc_now() >= deadline:
            qs = get_attempt_questions(existing["id"])
            persist_visible_answers(existing["id"], qs)
            mark_attempt_submitted(existing["id"], status="expired")
        else:
            return existing, None

    settings_rows = rows(db.table("exam_settings").select("*").eq("category", category).limit(1).execute())
    if not settings_rows or not settings_rows[0].get("is_active", True):
        return None, "This exam is currently disabled by the teacher."
    settings = settings_rows[0]
    available = rows(
        db.table("questions").select("*").eq("category", category).eq("is_active", True).execute()
    )
    wanted_type = {"aptitude": {"mcq", "short"}, "programming": {"coding"}}[category]
    available = [q for q in available if q.get("question_type") in wanted_type]
    if not available:
        return None, "There are no active questions in this section yet. Please contact the teacher."

    count = min(max(1, int(settings.get("questions_per_attempt") or 10)), len(available))

    # Prefer questions this student has not seen in prior attempts. This reduces repetition
    # until the eligible bank has been used; a small bank can still require repeats.
    past_attempts = rows(
        db.table("attempts").select("id")
        .eq("student_id", student["id"]).eq("category", category)
        .in_("status", ["submitted", "expired"]).limit(500).execute()
    )
    past_ids = [item["id"] for item in past_attempts]
    previously_seen: set[int] = set()
    if past_ids:
        past_assignments = rows(
            db.table("attempt_questions").select("question_id")
            .in_("attempt_id", past_ids).execute()
        )
        previously_seen = {int(item["question_id"]) for item in past_assignments}
    unseen = [q for q in available if int(q["id"]) not in previously_seen]
    if len(unseen) >= count:
        selected = random.sample(unseen, count)
    else:
        selected = list(unseen)
        remaining = count - len(selected)
        repeatable = [q for q in available if int(q["id"]) not in {int(item["id"]) for item in selected}]
        selected.extend(random.sample(repeatable, min(remaining, len(repeatable))))
    random.shuffle(selected)
    now = utc_now()
    duration = max(1, int(settings.get("duration_minutes") or 30))
    attempt_data = rows(
        db.table("attempts").insert(
            {
                "student_id": student["id"],
                "category": category,
                "status": "in_progress",
                "started_at": now.isoformat(),
                "deadline_at": (now + timedelta(minutes=duration)).isoformat(),
            }
        ).execute()
    )
    if not attempt_data:
        return None, "Could not create an attempt. Please try again."
    attempt = attempt_data[0]
    assignment_payload = [
        {"attempt_id": attempt["id"], "question_id": q["id"], "display_order": i, "marks": q.get("marks", 1)}
        for i, q in enumerate(selected, start=1)
    ]
    db.table("attempt_questions").insert(assignment_payload).execute()
    return attempt, None


def render_attempt(attempt_id: str) -> None:
    db = get_db()
    attempt_rows = rows(db.table("attempts").select("*").eq("id", attempt_id).limit(1).execute())
    if not attempt_rows:
        st.error("Attempt not found.")
        return
    attempt = attempt_rows[0]
    category = attempt["category"]
    qs = get_attempt_questions(attempt_id)

    if attempt.get("status") != "in_progress":
        st.title("Test result")
        st.info("This attempt has already been submitted or its time has expired.")
        left, right = st.columns(2)
        left.metric("Score", f"{float(attempt.get('score') or 0):g}")
        right.metric("Maximum marks", f"{float(attempt.get('max_score') or 0):g}")
        if st.button("Return to dashboard", type="primary"):
            st.session_state.pop("active_attempt_id", None)
            request_student_navigation("Dashboard")
            st.rerun()
        return

    deadline = parse_dt(attempt.get("deadline_at"))
    remaining = max(0, math.ceil((deadline - utc_now()).total_seconds())) if deadline else 0
    if remaining <= 0:
        persist_visible_answers(attempt_id, qs)
        mark_attempt_submitted(attempt_id, status="expired")
        st.warning("Time is up. Your attempt has been submitted.")
        st.rerun()
        return

    # One-second reruns keep the clock current; answers are also saved by widget callbacks.
    st_autorefresh(interval=1000, key=f"clock_{attempt_id}")
    mins, secs = divmod(remaining, 60)
    st.markdown(f"## {CATEGORY_LABELS[category]}")
    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.caption(f"Attempt ID: {attempt_id[:8]} · Questions: {len(qs)} · Autosave enabled")
    with top_right:
        st.metric("Time remaining", f"{mins:02d}:{secs:02d}")
    st.progress(min(1.0, max(0.0, remaining / max(1, (parse_dt(attempt.get("deadline_at")) - parse_dt(attempt.get("started_at"))).total_seconds()))))

    if st.session_state.get("answer_save_error"):
        st.warning("A response could not be saved to the database. Check your connection, then use Save draft. " + st.session_state["answer_save_error"][:180])

    for index, q in enumerate(qs, start=1):
        qid = int(q["id"])
        saved = q.get("saved_answer") or {}
        key_suffix = f"{attempt_id}_{qid}"
        st.markdown(f"### Question {index} · {q['attempt_marks']:g} mark(s)")
        st.write(q.get("prompt", ""))
        if q.get("topic"):
            st.caption(f"Topic: {q['topic']}")

        if q.get("question_type") == "mcq":
            options = q.get("options") or []
            key = f"answer_{key_suffix}"
            saved_value = saved.get("answer_text")
            if key not in st.session_state:
                st.session_state[key] = saved_value if saved_value in options else None
            st.radio(
                "Choose one answer", options=options, index=None,
                key=key, on_change=save_answer, args=(attempt_id, qid, "text"),
                label_visibility="collapsed",
            )
        elif q.get("question_type") == "short":
            key = f"answer_{key_suffix}"
            if key not in st.session_state:
                st.session_state[key] = saved.get("answer_text") or ""
            st.text_input(
                "Your answer", key=key, on_change=save_answer,
                args=(attempt_id, qid, "text"), placeholder="Enter a short answer",
            )
        else:
            code_key = f"code_{key_suffix}"
            output_key = f"output_{key_suffix}"
            if code_key not in st.session_state:
                st.session_state[code_key] = saved.get("code_answer") or ""
            if output_key not in st.session_state:
                st.session_state[output_key] = saved.get("output_answer") or ""
            st.text_area(
                "Your code (Python or the language specified in the question)",
                key=code_key, height=190, on_change=save_answer,
                args=(attempt_id, qid, "code"),
                placeholder="# Write your solution here",
            )
            st.text_area(
                "Output / result produced by your code", key=output_key, height=90,
                on_change=save_answer, args=(attempt_id, qid, "code"),
                placeholder="Paste the output you obtained",
            )
        st.divider()

    col_save, col_submit = st.columns([1, 1])
    with col_save:
        if st.button("Save draft", use_container_width=True):
            persist_visible_answers(attempt_id, qs)
            st.success("Responses saved.")
    with col_submit:
        with st.popover("Submit test", use_container_width=True):
            st.warning("You cannot change your answers after submitting.")
            if st.button("Confirm submission", type="primary", use_container_width=True):
                persist_visible_answers(attempt_id, qs)
                mark_attempt_submitted(attempt_id, status="submitted")
                st.session_state.pop("active_attempt_id", None)
                request_student_navigation("Dashboard")
                st.success("Test submitted.")
                st.rerun()


def get_student_attempts(student_id: str) -> list[dict[str, Any]]:
    return rows(
        get_db().table("attempts").select("*").eq("student_id", student_id)
        .order("started_at", desc=True).limit(100).execute()
    )


def render_student_dashboard(student: dict[str, Any]) -> None:
    st.markdown(f"<div class='hero'><h2>Welcome, {html.escape(str(student.get('full_name') or student['roll_number']))}</h2><p class='muted'>Choose a test, continue an attempt, or review your previous scores.</p></div>", unsafe_allow_html=True)
    cols = st.columns(2)
    db = get_db()
    configs = {row["category"]: row for row in rows(db.table("exam_settings").select("*").execute())}
    for col, category, icon in [(cols[0], "aptitude", "🧠"), (cols[1], "programming", "💻")]:
        config = configs.get(category, {})
        active = config.get("is_active", True)
        with col:
            st.markdown(f"### {icon} {CATEGORY_LABELS[category]}")
            st.write(config.get("title") or CATEGORY_LABELS[category])
            st.caption(f"Duration: {config.get('duration_minutes', 30)} min · Questions per attempt: {config.get('questions_per_attempt', 10)}")
            active_attempt = find_active_attempt(student["id"], category)
            if active_attempt:
                st.info("You have an attempt in progress. You can resume it.")
            if not active:
                st.caption("Currently disabled by the teacher.")
            if st.button("Resume test" if active_attempt else "Start test", key=f"start_{category}", disabled=not active, use_container_width=True, type="primary"):
                attempt, error = start_attempt(student, category)
                if error:
                    st.error(error)
                elif attempt:
                    st.session_state["active_attempt_id"] = attempt["id"]
                    request_student_navigation("Active test")
                    st.rerun()
    st.subheader("Recent results")
    attempts = [a for a in get_student_attempts(student["id"]) if a.get("status") != "in_progress"]
    if not attempts:
        st.info("Your submitted test results will appear here.")
    else:
        table = []
        for a in attempts[:10]:
            table.append({
                "Test": CATEGORY_LABELS.get(a.get("category"), a.get("category")),
                "Status": a.get("status", ""),
                "Score": a.get("score"),
                "Maximum": a.get("max_score"),
                "Started": a.get("started_at", "")[:16].replace("T", " "),
            })
        st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)


def render_student_results(student: dict[str, Any]) -> None:
    st.title("My test history")
    attempts = get_student_attempts(student["id"])
    if not attempts:
        st.info("No attempts yet.")
        return
    table = []
    for a in attempts:
        table.append({
            "Test": CATEGORY_LABELS.get(a.get("category"), a.get("category")),
            "Status": a.get("status", ""),
            "Score": a.get("score"),
            "Maximum": a.get("max_score"),
            "Started": a.get("started_at", "")[:19].replace("T", " "),
            "Submitted": (a.get("submitted_at") or "")[:19].replace("T", " "),
        })
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)


def question_label(q: dict[str, Any]) -> str:
    short = str(q.get("prompt", "")).replace("\n", " ")
    return f"#{q.get('id')} · {CATEGORY_LABELS.get(q.get('category'), q.get('category'))} · {TYPE_LABELS.get(q.get('question_type'), q.get('question_type'))} · {short[:68]}"


def render_question_bank() -> None:
    st.title("Question bank")
    st.caption("Add questions once and reuse them. Each new attempt receives a random selection from the active question bank.")
    add_tab, edit_tab, list_tab = st.tabs(["Add question", "Edit / deactivate", "Browse questions"])
    db = get_db()

    with add_tab:
        st.info("MCQ options: enter one option per line. For short answers, use | to separate accepted answers. Programming expected output is optional; without it, the teacher can grade manually.")
        with st.form("new_question_form", clear_on_submit=True):
            category = st.selectbox("Section", ["aptitude", "programming"], format_func=lambda x: CATEGORY_LABELS[x])
            qtype = st.selectbox("Question type", ["mcq", "short", "coding"], format_func=lambda x: TYPE_LABELS[x])
            prompt = st.text_area("Question text", height=120)
            topic = st.text_input("Topic (optional)")
            difficulty = st.selectbox("Difficulty", ["Easy", "Medium", "Hard"])
            marks = st.number_input("Marks", min_value=0.5, max_value=100.0, value=1.0, step=0.5)
            options_text = st.text_area("MCQ options (one per line)", height=100)
            correct_answer = st.text_input("Correct answer / accepted answers (separate alternatives with |)")
            expected_output = st.text_area("Expected programming output (optional)", height=80)
            add_submitted = st.form_submit_button("Add question", type="primary", use_container_width=True)
        if add_submitted:
            if not prompt.strip():
                st.error("Question text is required.")
            elif (category == "aptitude" and qtype == "coding") or (category == "programming" and qtype != "coding"):
                st.error("Aptitude questions must be MCQ or short answer. Programming questions must use the programming/code type.")
            else:
                options = [line.strip() for line in options_text.splitlines() if line.strip()] if qtype == "mcq" else []
                if qtype == "mcq" and (len(options) < 2 or not correct_answer.strip() or correct_answer.strip() not in options):
                    st.error("For an MCQ, enter at least two options and set the correct answer to exactly one of them.")
                elif qtype == "short" and not correct_answer.strip():
                    st.error("A short-answer question needs at least one accepted answer.")
                else:
                    db.table("questions").insert({
                        "category": category, "question_type": qtype, "prompt": prompt.strip(),
                        "topic": topic.strip() or None, "difficulty": difficulty,
                        "marks": float(marks), "options": options,
                        "correct_answer": correct_answer.strip() or None,
                        "expected_output": expected_output if qtype == "coding" and expected_output.strip() else None,
                        "is_active": True,
                    }).execute()
                    st.success("Question added.")
                    st.rerun()

    all_questions = rows(db.table("questions").select("*").order("id", desc=True).limit(1000).execute())
    with edit_tab:
        if not all_questions:
            st.info("No questions yet.")
        else:
            q_map = {question_label(q): q for q in all_questions}
            selected_label = st.selectbox("Choose a question", list(q_map.keys()))
            q = q_map[selected_label]
            st.caption(f"Category and type are fixed after creation so historical attempts stay consistent. Add a new question to change either.")
            with st.form(f"edit_question_{q['id']}"):
                prompt_edit = st.text_area("Question text", value=q.get("prompt", ""), height=120)
                topic_edit = st.text_input("Topic", value=q.get("topic") or "")
                difficulty_edit = st.selectbox("Difficulty", ["Easy", "Medium", "Hard"], index=["Easy", "Medium", "Hard"].index(q.get("difficulty") if q.get("difficulty") in ["Easy", "Medium", "Hard"] else "Medium"))
                marks_edit = st.number_input("Marks", min_value=0.5, max_value=100.0, value=float(q.get("marks") or 1), step=0.5)
                options_edit = st.text_area("MCQ options (one per line)", value="\n".join(q.get("options") or []), height=100)
                correct_edit = st.text_input("Correct / accepted answer(s)", value=q.get("correct_answer") or "")
                expected_edit = st.text_area("Expected programming output", value=q.get("expected_output") or "", height=80)
                save_edit = st.form_submit_button("Save changes", type="primary")
            if save_edit:
                options = [line.strip() for line in options_edit.splitlines() if line.strip()] if q.get("question_type") == "mcq" else (q.get("options") or [])
                if not prompt_edit.strip():
                    st.error("Question text is required.")
                elif q.get("question_type") == "mcq" and (len(options) < 2 or correct_edit.strip() not in options):
                    st.error("MCQ needs at least two options and a correct answer that exactly matches an option.")
                elif q.get("question_type") == "short" and not correct_edit.strip():
                    st.error("A short-answer question needs an accepted answer.")
                else:
                    db.table("questions").update({
                        "prompt": prompt_edit.strip(), "topic": topic_edit.strip() or None,
                        "difficulty": difficulty_edit, "marks": float(marks_edit), "options": options,
                        "correct_answer": correct_edit.strip() or None,
                        "expected_output": expected_edit if q.get("question_type") == "coding" and expected_edit.strip() else None,
                        "updated_at": utc_now().isoformat(),
                    }).eq("id", q["id"]).execute()
                    st.success("Question updated.")
                    st.rerun()
            if q.get("is_active"):
                if st.button("Deactivate question", key=f"deactivate_{q['id']}"):
                    db.table("questions").update({"is_active": False, "updated_at": utc_now().isoformat()}).eq("id", q["id"]).execute()
                    st.success("Question deactivated. Past attempts are preserved.")
                    st.rerun()
            else:
                if st.button("Reactivate question", key=f"activate_{q['id']}"):
                    db.table("questions").update({"is_active": True, "updated_at": utc_now().isoformat()}).eq("id", q["id"]).execute()
                    st.success("Question reactivated.")
                    st.rerun()

    with list_tab:
        filter_category = st.selectbox("Filter section", ["All", "aptitude", "programming"], format_func=lambda x: "All sections" if x == "All" else CATEGORY_LABELS[x], key="question_filter")
        filtered = all_questions if filter_category == "All" else [q for q in all_questions if q.get("category") == filter_category]
        st.dataframe(pd.DataFrame([{
            "ID": q.get("id"), "Section": CATEGORY_LABELS.get(q.get("category"), q.get("category")),
            "Type": TYPE_LABELS.get(q.get("question_type"), q.get("question_type")),
            "Topic": q.get("topic"), "Marks": q.get("marks"), "Active": q.get("is_active"),
            "Question": q.get("prompt"),
        } for q in filtered]), use_container_width=True, hide_index=True)


def render_exam_settings() -> None:
    st.title("Exam settings")
    db = get_db()
    current = {r["category"]: r for r in rows(db.table("exam_settings").select("*").execute())}
    for category in ("aptitude", "programming"):
        setting = current.get(category, {})
        with st.expander(CATEGORY_LABELS[category], expanded=True):
            with st.form(f"settings_{category}"):
                title = st.text_input("Display title", value=setting.get("title") or CATEGORY_LABELS[category], key=f"title_{category}")
                duration = st.number_input("Duration (minutes)", min_value=1, max_value=300, value=int(setting.get("duration_minutes") or 30), key=f"duration_{category}")
                count = st.number_input("Questions per attempt", min_value=1, max_value=200, value=int(setting.get("questions_per_attempt") or 10), key=f"count_{category}")
                active = st.checkbox("Allow students to start this exam", value=bool(setting.get("is_active", True)), key=f"active_{category}")
                submitted = st.form_submit_button("Save settings", type="primary")
            if submitted:
                db.table("exam_settings").upsert({
                    "category": category, "title": title.strip() or CATEGORY_LABELS[category],
                    "duration_minutes": int(duration), "questions_per_attempt": int(count),
                    "is_active": bool(active), "updated_at": utc_now().isoformat(),
                }, on_conflict="category").execute()
                st.success(f"{CATEGORY_LABELS[category]} settings saved.")
                st.rerun()


def render_students() -> None:
    st.title("Student management")
    st.caption("Student PINs are hashed before storage. Importing an existing roll number updates that student's name and PIN.")
    db = get_db()
    with st.expander("Add one student", expanded=True):
        with st.form("add_student_form", clear_on_submit=True):
            roll = st.text_input("Roll number")
            name = st.text_input("Full name")
            pin = st.text_input("Student PIN", type="password", help="Use a unique PIN. Avoid reusing personal passwords.")
            add = st.form_submit_button("Create / update student", type="primary")
        if add:
            if not roll.strip() or not name.strip() or len(pin) < 4:
                st.error("Enter roll number, full name, and a PIN of at least 4 characters.")
            else:
                db.table("students").upsert({
                    "roll_number": roll.strip().upper(), "full_name": name.strip(),
                    "pin_hash": generate_password_hash(pin), "is_active": True,
                }, on_conflict="roll_number").execute()
                st.success("Student account saved.")
                st.rerun()

    st.subheader("Import roster CSV")
    st.write("Required columns: `roll_number`, `full_name`, `pin`. Keep PINs as text in the CSV so leading zeroes are preserved.")
    uploaded = st.file_uploader("Select roster CSV", type=["csv"], key="roster_csv")
    if uploaded is not None:
        try:
            contents = uploaded.getvalue().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(contents))
            required = {"roll_number", "full_name", "pin"}
            if not reader.fieldnames or not required.issubset({h.strip() for h in reader.fieldnames}):
                st.error("CSV must include roll_number, full_name, and pin columns.")
            else:
                records = []
                for line_number, row in enumerate(reader, start=2):
                    normalized = {str(k).strip(): str(v or "").strip() for k, v in row.items() if k is not None}
                    roll_n = normalized.get("roll_number", "").upper()
                    name_n = normalized.get("full_name", "")
                    pin_n = normalized.get("pin", "")
                    if not roll_n or not name_n or len(pin_n) < 4:
                        st.warning(f"Skipped CSV line {line_number}: missing required values or PIN shorter than 4 characters.")
                        continue
                    records.append({
                        "roll_number": roll_n, "full_name": name_n,
                        "pin_hash": generate_password_hash(pin_n), "is_active": True,
                    })
                st.write(f"Valid student rows: **{len(records)}**")
                if st.button("Import valid students", type="primary", disabled=not records):
                    for start in range(0, len(records), 100):
                        db.table("students").upsert(records[start:start + 100], on_conflict="roll_number").execute()
                    st.success(f"Imported or updated {len(records)} student accounts.")
                    st.rerun()
        except UnicodeDecodeError:
            st.error("Could not read this file as UTF-8 CSV. Save it as UTF-8 and try again.")

    students = rows(db.table("students").select("id,roll_number,full_name,is_active,created_at").order("roll_number").limit(2000).execute())
    st.subheader(f"Student roster ({len(students)})")
    if students:
        st.dataframe(pd.DataFrame([{
            "Roll number": s.get("roll_number"), "Name": s.get("full_name"),
            "Active": s.get("is_active"), "Created": (s.get("created_at") or "")[:10],
        } for s in students]), use_container_width=True, hide_index=True)
        selected_roll = st.selectbox("Enable / disable student account", [s["roll_number"] for s in students])
        selected = next(s for s in students if s["roll_number"] == selected_roll)
        if selected.get("is_active"):
            if st.button("Disable selected student"):
                db.table("students").update({"is_active": False}).eq("id", selected["id"]).execute()
                st.success("Student disabled.")
                st.rerun()
        elif st.button("Enable selected student"):
            db.table("students").update({"is_active": True}).eq("id", selected["id"]).execute()
            st.success("Student enabled.")
            st.rerun()


def recalculate_attempt_score(attempt_id: str) -> None:
    db = get_db()
    answers = rows(db.table("answers").select("awarded_marks").eq("attempt_id", attempt_id).execute())
    assignments = rows(db.table("attempt_questions").select("marks").eq("attempt_id", attempt_id).execute())
    score = sum(float(a.get("awarded_marks") or 0) for a in answers)
    maximum = sum(float(a.get("marks") or 0) for a in assignments)
    db.table("attempts").update({"score": round(score, 2), "max_score": round(maximum, 2)}).eq("id", attempt_id).execute()


def render_results_admin() -> None:
    st.title("Results & grading")
    db = get_db()
    attempts = rows(db.table("attempts").select("*").order("started_at", desc=True).limit(1000).execute())
    if not attempts:
        st.info("Student attempts will appear here.")
        return
    student_ids = list({a["student_id"] for a in attempts if a.get("student_id")})
    students = rows(db.table("students").select("id,roll_number,full_name").in_("id", student_ids).execute()) if student_ids else []
    students_by_id = {s["id"]: s for s in students}
    report = []
    for a in attempts:
        s = students_by_id.get(a.get("student_id"), {})
        report.append({
            "attempt_id": a.get("id"), "Roll number": s.get("roll_number", ""),
            "Student": s.get("full_name", ""), "Test": CATEGORY_LABELS.get(a.get("category"), a.get("category")),
            "Status": a.get("status"), "Score": a.get("score"), "Maximum": a.get("max_score"),
            "Started": (a.get("started_at") or "")[:19].replace("T", " "),
        })
    df = pd.DataFrame(report)
    csv_bytes = df.drop(columns=["attempt_id"]).to_csv(index=False).encode("utf-8")
    st.download_button("Download results CSV", data=csv_bytes, file_name="test_results.csv", mime="text/csv")
    st.dataframe(df.drop(columns=["attempt_id"]), use_container_width=True, hide_index=True)

    completed = [a for a in attempts if a.get("status") in ("submitted", "expired")]
    if not completed:
        st.info("Submitted attempts will become available for review here.")
        return
    choices = {}
    for a in completed:
        s = students_by_id.get(a.get("student_id"), {})
        key = f"{s.get('roll_number', 'unknown')} · {CATEGORY_LABELS.get(a.get('category'), a.get('category'))} · {str(a.get('started_at', ''))[:16]} · {a['id'][:8]}"
        choices[key] = a
    st.subheader("Review a submission")
    selected_label = st.selectbox("Select an attempt", list(choices.keys()))
    attempt = choices[selected_label]
    qs = get_attempt_questions(attempt["id"])
    for number, q in enumerate(qs, start=1):
        saved = q.get("saved_answer") or {}
        with st.expander(f"Q{number}. {str(q.get('prompt', ''))[:120]} · {q.get('attempt_marks', 0):g} mark(s)"):
            st.write(q.get("prompt", ""))
            if q.get("question_type") == "coding":
                st.markdown("**Submitted code**")
                st.code(saved.get("code_answer") or "(No code submitted)", language="python")
                st.markdown("**Student-provided output**")
                st.code(saved.get("output_answer") or "(No output submitted)")
                if q.get("expected_output"):
                    st.markdown("**Expected output**")
                    st.code(q.get("expected_output"))
            else:
                st.write("**Student answer:**", saved.get("answer_text") or "(Blank)")
                st.write("**Answer key:**", q.get("correct_answer") or "Not configured")
            current_award = float(saved.get("awarded_marks") or 0)
            with st.form(f"grade_{attempt['id']}_{q['id']}"):
                awarded = st.number_input(
                    "Marks awarded", min_value=0.0, max_value=float(q.get("attempt_marks") or 0),
                    value=min(current_award, float(q.get("attempt_marks") or 0)), step=0.5,
                    key=f"grade_value_{attempt['id']}_{q['id']}",
                )
                save_grade = st.form_submit_button("Save grade")
            if save_grade:
                db.table("answers").upsert({
                    "attempt_id": attempt["id"], "question_id": q["id"],
                    "answer_text": saved.get("answer_text"), "code_answer": saved.get("code_answer"),
                    "output_answer": saved.get("output_answer"), "awarded_marks": float(awarded),
                    "is_correct": bool(awarded >= float(q.get("attempt_marks") or 0)) if float(q.get("attempt_marks") or 0) > 0 else None,
                    "graded_at": utc_now().isoformat(),
                }, on_conflict="attempt_id,question_id").execute()
                recalculate_attempt_score(attempt["id"])
                st.success("Grade saved and total recalculated.")
                st.rerun()


def render_admin_dashboard() -> None:
    st.markdown("<div class='hero'><h2>Teacher / Admin dashboard</h2><p class='muted'>Manage the question bank, student roster, exam settings, and submissions.</p></div>", unsafe_allow_html=True)
    tab_questions, tab_settings, tab_students, tab_results = st.tabs(["Question bank", "Exam settings", "Students", "Results & grading"])
    with tab_questions:
        render_question_bank()
    with tab_settings:
        render_exam_settings()
    with tab_students:
        render_students()
    with tab_results:
        render_results_admin()


def login_screen() -> None:
    st.markdown("<div class='hero'><h1>📝 Class Test Portal</h1><p class='muted'>A single place for aptitude practice and programming assessments.</p></div>", unsafe_allow_html=True)
    student_tab, admin_tab = st.tabs(["Student login", "Admin login"])
    with student_tab:
        with st.form("student_login_form"):
            roll = st.text_input("Roll number").strip().upper()
            pin = st.text_input("PIN", type="password")
            submit = st.form_submit_button("Login as student", type="primary", use_container_width=True)
        if submit:
            try:
                matches = rows(get_db().table("students").select("*").eq("roll_number", roll).eq("is_active", True).limit(1).execute())
                if matches and check_password_hash(matches[0].get("pin_hash", ""), pin):
                    student = matches[0]
                    st.session_state["user"] = {"role": "student", "id": student["id"], "roll_number": student["roll_number"], "full_name": student.get("full_name") or student["roll_number"]}
                    st.session_state["page"] = "Dashboard"
                    st.rerun()
                st.error("Invalid roll number or PIN, or the account is disabled.")
            except Exception as exc:
                st.error(f"Login failed: {exc}")
    with admin_tab:
        with st.form("admin_login_form"):
            username = st.text_input("Admin username")
            password = st.text_input("Admin password", type="password")
            submit_admin = st.form_submit_button("Login as admin", type="primary", use_container_width=True)
        if submit_admin:
            admin_username = secret("ADMIN_USERNAME", "admin")
            admin_hash = secret("ADMIN_PASSWORD_HASH")
            if not admin_hash:
                st.error("Admin password hash is not configured. Follow the setup instructions in README.md.")
            elif username == admin_username and check_password_hash(admin_hash, password):
                st.session_state["user"] = {"role": "admin", "username": admin_username}
                st.session_state["page"] = "Admin"
                st.rerun()
            else:
                st.error("Invalid admin credentials.")


def main() -> None:
    st.sidebar.title("Class Test Portal")
    if not supabase_configured():
        st.warning("Database configuration is missing. Set up Supabase and add the values shown in `.streamlit/secrets.toml.example`.")
        st.markdown("Start with the `README.md` and run `schema.sql` in the Supabase SQL Editor.")
        st.stop()

    user = st.session_state.get("user")
    if not user:
        login_screen()
        st.sidebar.caption("Student and teacher access")
        return

    if user.get("role") == "admin":
        st.sidebar.success(f"Admin: {user.get('username')}")
        if st.sidebar.button("Log out", use_container_width=True):
            st.session_state.clear()
            st.rerun()
        try:
            render_admin_dashboard()
        except Exception as exc:
            st.error(f"Admin page error: {exc}")
            st.caption("Check the Supabase schema and secrets. See README.md for setup and troubleshooting.")
        return

    st.sidebar.success(f"{user.get('full_name')} · {user.get('roll_number')}")
    # Widget-bound keys cannot be assigned after that widget is instantiated.
    # Internal navigation requests are applied here, before the radio is created.
    nav_target = st.session_state.pop("student_navigation_target", None)
    if nav_target in ("Dashboard", "My results", "Active test"):
        st.session_state["student_navigation"] = nav_target
    page = st.sidebar.radio("Navigate", ["Dashboard", "My results", "Active test"], key="student_navigation")
    if st.sidebar.button("Log out", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    if page == "Dashboard":
        render_student_dashboard(user)
    elif page == "My results":
        render_student_results(user)
    else:
        attempt_id = st.session_state.get("active_attempt_id")
        if attempt_id:
            try:
                render_attempt(attempt_id)
            except Exception as exc:
                st.error(f"Could not load attempt: {exc}")
                st.caption("Your responses are saved as you change them. Check the connection and reload the page.")
        else:
            st.info("Start a test from the dashboard first.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        st.error(f"Application error: {exc}")
        st.caption("Verify the Supabase schema and configuration using README.md.")
