"""Exam engine router.

Spec §5. The student lifecycle on exam day:

  POST /exam/start               -> create or resume an ExamSession
  GET  /exam/{session_id}        -> session state (status, time left, counts)
  GET  /exam/{session_id}/questions  -> ordered questions, choices stripped
  POST /exam/{session_id}/answer     -> save an answer (idempotent per question)
  POST /exam/{session_id}/submit     -> finalize and grade

Scoring (spec §1.4): each Question has `item_points`; each Choice has a
`weight` of 1/choices_count when correct else 0. For 'pg' and 'tf' (single
correct), score = item_points if the chosen choice is correct else 0. For
'complex_mc' (multi-select), score = clamp(sum(selected weights), 0, 1) *
item_points so partial-correct gets partial credit but extra wrong picks
don't go negative.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, utcnow
from models import (
    AnswerChoice, Choice, Exam, ExamResult, ExamSession,
    Question, Student, StudentAnswer,
)
from routers.auth import get_current_student

router = APIRouter(prefix="/exam", tags=["exam"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StartExamRequest(BaseModel):
    exam_id: str


class ChoiceOut(BaseModel):
    id: str
    body: str


class QuestionOut(BaseModel):
    id: str
    question_type: str       # 'pg' | 'tf' | 'complex_mc'
    body: str
    image_url: Optional[str]
    choices_count: int
    choices: list[ChoiceOut]  # is_correct/weight intentionally omitted


class SessionState(BaseModel):
    session_id: str
    exam_id: str
    exam_title: str
    status: str
    time_remaining_seconds: int
    questions_total: int
    questions_answered: int
    violation_count: int
    locked_until: Optional[datetime]


class QuestionsResponse(BaseModel):
    session_id: str
    questions: list[QuestionOut]


class AnswerRequest(BaseModel):
    question_id: str
    choice_ids: list[str] = Field(default_factory=list)


class AnswerResponse(BaseModel):
    saved: bool
    question_id: str
    selected_count: int


class SubmitResponse(BaseModel):
    session_id: str
    total_score: float
    max_score: float
    percentage: float
    submitted_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_session(db: Session, session_id: str, student: Student) -> ExamSession:
    s = db.query(ExamSession).filter_by(id=session_id).first()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    if s.student_id != student.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not your session")
    return s


def _exam_window_end(exam: Exam, started_at: datetime) -> datetime:
    """A session ends at min(scheduled_at + duration, time_end-of-day)."""
    duration_end = started_at + timedelta(minutes=exam.duration_minutes)
    end_of_day = datetime.combine(exam.scheduled_at.date(), exam.time_end)
    return min(duration_end, end_of_day)


def _time_remaining(s: ExamSession) -> int:
    if s.status not in {"active", "pending"}:
        return 0
    if s.started_at is None:
        return s.exam.duration_minutes * 60
    deadline = _exam_window_end(s.exam, s.started_at)
    remaining = (deadline - utcnow()).total_seconds()
    return max(0, int(remaining))


def _score_question(q: Question, selected_choice_ids: set[str]) -> float:
    """Spec §1.4 scoring. Returns points earned for this question (0..item_points)."""
    if not selected_choice_ids:
        return 0.0
    by_id = {c.id: c for c in q.choices}
    if q.question_type in {"pg", "tf"}:
        # Single-correct: any single matching pick earns full points; any
        # wrong pick (or multi-pick on a single-correct question) earns 0.
        if len(selected_choice_ids) != 1:
            return 0.0
        ch = by_id.get(next(iter(selected_choice_ids)))
        return q.item_points if ch is not None and ch.is_correct else 0.0
    # complex_mc: sum weights of selected choices, clamp to [0, 1].
    total_weight = 0.0
    for cid in selected_choice_ids:
        ch = by_id.get(cid)
        if ch is None:
            continue
        total_weight += ch.weight if ch.is_correct else -ch.weight
    fraction = max(0.0, min(1.0, total_weight))
    return q.item_points * fraction


# ---------------------------------------------------------------------------
# §5.1  POST /exam/start
# ---------------------------------------------------------------------------

@router.post("/start", response_model=SessionState)
def start_exam(
    body: StartExamRequest,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    exam = db.query(Exam).filter_by(id=body.exam_id).first()
    if exam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="exam not found")
    if not exam.admin_confirmed:
        # Spec §1.3: an exam must be admin-confirmed before students can start.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="exam is not yet open (admin has not confirmed)",
        )

    now = utcnow()
    if now < exam.scheduled_at:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"exam starts at {exam.scheduled_at.isoformat()}",
        )
    end_of_day = datetime.combine(exam.scheduled_at.date(), exam.time_end)
    if now > end_of_day:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="exam window has closed",
        )

    # Resume an existing session if there is one (idempotent start).
    s = (
        db.query(ExamSession)
        .filter_by(student_id=student.id, exam_id=exam.id)
        .first()
    )
    if s is None:
        question_ids = [q.id for q in exam.questions]
        random.shuffle(question_ids)  # per-student randomized order
        s = ExamSession(
            student_id=student.id,
            exam_id=exam.id,
            status="active",
            started_at=now,
            question_order=question_ids,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
    else:
        if s.status == "submitted":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="exam already submitted",
            )
        if s.status == "expelled":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail="expelled from this exam",
            )
        if s.status == "pending":
            s.status = "active"
            s.started_at = s.started_at or now
            db.commit()
            db.refresh(s)

    return _state_for(s, db)


# ---------------------------------------------------------------------------
# §5.2  GET /exam/{session_id}  (state)
# ---------------------------------------------------------------------------

@router.get("/{session_id}", response_model=SessionState)
def session_state(
    session_id: str,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    s = _load_session(db, session_id, student)
    return _state_for(s, db)


def _state_for(s: ExamSession, db: Session) -> SessionState:
    answered = (
        db.query(StudentAnswer).filter_by(session_id=s.id).count()
    )
    total = len(s.question_order or []) or len(s.exam.questions)
    return SessionState(
        session_id=s.id,
        exam_id=s.exam_id,
        exam_title=s.exam.title,
        status=s.status,
        time_remaining_seconds=_time_remaining(s),
        questions_total=total,
        questions_answered=answered,
        violation_count=s.violation_count,
        locked_until=s.locked_until,
    )


# ---------------------------------------------------------------------------
# §5.3  GET /exam/{session_id}/questions
# ---------------------------------------------------------------------------

@router.get("/{session_id}/questions", response_model=QuestionsResponse)
def list_questions(
    session_id: str,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    s = _load_session(db, session_id, student)
    if s.status not in {"active", "pending"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"session is {s.status}, cannot fetch questions",
        )
    if s.locked_until and s.locked_until > utcnow():
        raise HTTPException(
            status.HTTP_423_LOCKED,
            detail=f"session locked until {s.locked_until.isoformat()}",
        )

    order = s.question_order or [q.id for q in s.exam.questions]
    q_by_id = {q.id: q for q in s.exam.questions}

    out: list[QuestionOut] = []
    for qid in order:
        q = q_by_id.get(qid)
        if q is None:
            # Question was deleted after the session started; skip it.
            continue
        out.append(QuestionOut(
            id=q.id,
            question_type=q.question_type,
            body=q.body,
            image_url=q.image_url,
            choices_count=q.choices_count,
            # Strip is_correct/weight before sending to the student.
            choices=[ChoiceOut(id=c.id, body=c.body) for c in q.choices],
        ))

    return QuestionsResponse(session_id=s.id, questions=out)


# ---------------------------------------------------------------------------
# §5.4  POST /exam/{session_id}/answer
# ---------------------------------------------------------------------------

@router.post("/{session_id}/answer", response_model=AnswerResponse)
def save_answer(
    session_id: str,
    body: AnswerRequest,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    s = _load_session(db, session_id, student)
    if s.status not in {"active", "pending"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"session is {s.status}, cannot save answers",
        )
    if s.locked_until and s.locked_until > utcnow():
        raise HTTPException(
            status.HTTP_423_LOCKED,
            detail=f"session locked until {s.locked_until.isoformat()}",
        )

    q = db.query(Question).filter_by(id=body.question_id).first()
    if q is None or q.exam_id != s.exam_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="question not in this exam",
        )

    valid_choice_ids = {c.id for c in q.choices}
    requested = set(body.choice_ids)
    if not requested.issubset(valid_choice_ids):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="one or more choice_ids do not belong to this question",
        )
    if q.question_type in {"pg", "tf"} and len(requested) > 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{q.question_type} question accepts at most one choice",
        )

    # Upsert StudentAnswer — UNIQUE(session_id, question_id) guarantees one row.
    sa = (
        db.query(StudentAnswer)
        .filter_by(session_id=s.id, question_id=q.id)
        .first()
    )
    if sa is None:
        sa = StudentAnswer(session_id=s.id, question_id=q.id)
        db.add(sa)
        db.flush()
    else:
        # Replace prior selections.
        db.query(AnswerChoice).filter_by(student_answer_id=sa.id).delete()

    for cid in requested:
        db.add(AnswerChoice(student_answer_id=sa.id, choice_id=cid))

    db.commit()
    return AnswerResponse(
        saved=True, question_id=q.id, selected_count=len(requested),
    )


# ---------------------------------------------------------------------------
# §5.5  POST /exam/{session_id}/submit
# ---------------------------------------------------------------------------

@router.post("/{session_id}/submit", response_model=SubmitResponse)
def submit_exam(
    session_id: str,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    s = _load_session(db, session_id, student)
    if s.status == "submitted":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="already submitted",
        )
    if s.status == "expelled":
        # Expelled sessions are auto-finalized by the violation handler,
        # not by the student. They should never reach this endpoint.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="session was expelled",
        )

    questions = list(s.exam.questions)
    answers_by_qid = {a.question_id: a for a in s.answers}

    total_score = 0.0
    max_score = 0.0
    for q in questions:
        max_score += q.item_points
        sa = answers_by_qid.get(q.id)
        selected = (
            {ac.choice_id for ac in sa.answer_choices} if sa is not None else set()
        )
        earned = _score_question(q, selected)
        if sa is not None:
            sa.score_earned = earned
        total_score += earned

    s.status = "submitted"
    s.submitted_at = utcnow()

    # Replace any prior ExamResult (defensive — shouldn't exist since we
    # block re-submit above).
    if s.result is not None:
        db.delete(s.result)
        db.flush()
    db.add(ExamResult(
        session_id=s.id,
        total_score=total_score,
        max_score=max_score,
    ))

    db.commit()

    pct = (total_score / max_score * 100.0) if max_score > 0 else 0.0
    return SubmitResponse(
        session_id=s.id,
        total_score=total_score,
        max_score=max_score,
        percentage=round(pct, 2),
        submitted_at=s.submitted_at,
    )