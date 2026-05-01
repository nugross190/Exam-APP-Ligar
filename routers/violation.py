"""Violation tracking and panic button.

Spec §6.

  POST /violation/{session_id}      (student) - record a violation event
  POST /violation/{session_id}/panic (student) - emergency exit
  GET  /violation/{session_id}      (homeroom/admin) - per-session history

Threshold (spec §6.2):
  count == 1 -> warning
  count == 2 -> 30s lockout (locked_until set)
  count >= 3 -> session expelled, ExpelledFlag created and surfaced to
                the homeroom teacher of the student's class.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import (
    ExamSession, ExpelledFlag, SessionViolation, Student, Teacher,
)
from routers.auth import get_current_student, get_current_teacher

router = APIRouter(prefix="/violation", tags=["violation"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_ALLOWED_EVENTS = {"tab_switch", "fullscreen_exit"}
_LOCKOUT_SECONDS = 30
_EXPEL_THRESHOLD = 3


class ViolationRequest(BaseModel):
    event_type: str  # 'tab_switch' | 'fullscreen_exit'


class ViolationResponse(BaseModel):
    session_id: str
    violation_count: int
    status: str                              # 'active' | 'expelled'
    locked_until: Optional[datetime]
    expelled: bool
    message: str


class PanicResponse(BaseModel):
    session_id: str
    status: str
    panic_at: datetime


class ViolationRow(BaseModel):
    id: str
    event_type: str
    occurred_at: datetime


class ViolationHistoryResponse(BaseModel):
    session_id: str
    violation_count: int
    status: str
    violations: list[ViolationRow]


# ---------------------------------------------------------------------------
# §6.1  POST /violation/{session_id}
# ---------------------------------------------------------------------------

@router.post("/{session_id}", response_model=ViolationResponse)
def record_violation(
    session_id: str,
    body: ViolationRequest,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    if body.event_type not in _ALLOWED_EVENTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"event_type must be one of {sorted(_ALLOWED_EVENTS)}",
        )

    s = db.query(ExamSession).filter_by(id=session_id).first()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    if s.student_id != student.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not your session")
    if s.status not in {"active", "pending"}:
        # Already terminal — don't accumulate further violations against
        # a submitted/expelled/panic session.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"session is {s.status}, violations not accepted",
        )

    db.add(SessionViolation(
        session_id=s.id,
        event_type=body.event_type,
        occurred_at=datetime.utcnow(),
    ))
    s.violation_count = (s.violation_count or 0) + 1

    expelled = False
    message = "warning recorded"
    if s.violation_count >= _EXPEL_THRESHOLD:
        s.status = "expelled"
        s.submitted_at = s.submitted_at or datetime.utcnow()
        # Surface to homeroom teacher (spec §6.2).
        db.add(ExpelledFlag(
            session_id=s.id,
            student_id=s.student_id,
            class_id=student.class_id,
        ))
        expelled = True
        message = "expelled: violation threshold reached"
    elif s.violation_count == 2:
        s.locked_until = datetime.utcnow() + timedelta(seconds=_LOCKOUT_SECONDS)
        message = f"second violation: locked for {_LOCKOUT_SECONDS}s"

    db.commit()
    db.refresh(s)

    return ViolationResponse(
        session_id=s.id,
        violation_count=s.violation_count,
        status=s.status,
        locked_until=s.locked_until,
        expelled=expelled,
        message=message,
    )


# ---------------------------------------------------------------------------
# §6.3  POST /violation/{session_id}/panic
# ---------------------------------------------------------------------------

@router.post("/{session_id}/panic", response_model=PanicResponse)
def panic(
    session_id: str,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    s = db.query(ExamSession).filter_by(id=session_id).first()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    if s.student_id != student.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not your session")
    if s.status in {"submitted", "expelled", "panic"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"session is already {s.status}",
        )

    s.status = "panic"
    s.panic_at = datetime.utcnow()
    db.commit()
    db.refresh(s)

    return PanicResponse(
        session_id=s.id, status=s.status, panic_at=s.panic_at,
    )


# ---------------------------------------------------------------------------
# §6.4  GET /violation/{session_id}  (homeroom/admin view)
# ---------------------------------------------------------------------------

@router.get("/{session_id}", response_model=ViolationHistoryResponse)
def violation_history(
    session_id: str,
    teacher: Teacher = Depends(get_current_teacher),
    db: Session = Depends(get_db),
):
    s = db.query(ExamSession).filter_by(id=session_id).first()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")

    # Homeroom teachers only see sessions of students in their own class.
    # Admin/owner roles see everything.
    if teacher.role == "homeroom":
        if s.student.class_.homeroom_teacher_id != teacher.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="not the homeroom teacher of this student's class",
            )

    rows = (
        db.query(SessionViolation)
        .filter_by(session_id=s.id)
        .order_by(SessionViolation.occurred_at)
        .all()
    )
    return ViolationHistoryResponse(
        session_id=s.id,
        violation_count=s.violation_count,
        status=s.status,
        violations=[
            ViolationRow(id=v.id, event_type=v.event_type, occurred_at=v.occurred_at)
            for v in rows
        ],
    )