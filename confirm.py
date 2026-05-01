"""Data confirmation router.

Spec §9. Open during testing week (3rd week May). Students verify their
subject list before exam week (1st week June).

  GET  /confirm/my-subjects        (student) - what they'll be examined on
  POST /confirm/flag-error         (student) - "this list is wrong"
  POST /confirm/confirm            (student) - "this list is correct"
  GET  /confirm/homeroom-summary   (homeroom teacher) - per-student status
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Class, ClassSubject, DataFlag, Exam, Student, Subject, Teacher
from routers.auth import get_current_student, get_current_teacher

router = APIRouter(prefix="/confirm", tags=["confirm"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SubjectExamSlot(BaseModel):
    name: str
    exam_date: Optional[str]   # 'YYYY-MM-DD' or None if no Exam scheduled
    time_start: Optional[str]  # 'HH:MM' or None


class MySubjectsResponse(BaseModel):
    student_name: str
    class_name: str
    data_confirmed: bool
    subjects: list[SubjectExamSlot]


class FlagErrorRequest(BaseModel):
    note: str


class HomeroomStudentRow(BaseModel):
    student_id: str
    name: str
    nisn: str
    nis: str
    subject_count: int
    data_confirmed: bool
    flagged: bool
    flag_reason: Optional[str]


class HomeroomSummaryResponse(BaseModel):
    class_name: str
    students: list[HomeroomStudentRow]


# ---------------------------------------------------------------------------
# §9.1  GET /confirm/my-subjects
# ---------------------------------------------------------------------------

@router.get("/my-subjects", response_model=MySubjectsResponse)
def my_subjects(
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    # Walk: ClassSubject -> Subject -> Exam (subject_id).
    # Spec doesn't strictly require the Exam join, but the response
    # includes exam_date + time_start, so we need it.
    rows = (
        db.query(Subject, Exam)
        .join(ClassSubject, ClassSubject.subject_id == Subject.id)
        .outerjoin(Exam, Exam.subject_id == Subject.id)
        # ^ outer join: a Subject without a scheduled Exam still shows
        # up in the student's list, just with null date/time. Means
        # students can spot a missing-exam case during confirmation.
        .filter(ClassSubject.class_id == student.class_id)
        .order_by(Exam.scheduled_at.is_(None), Exam.scheduled_at, Subject.name)
        # ^ scheduled exams first (sorted by date), then unscheduled ones
        .all()
    )

    subjects = []
    for subj, exam in rows:
        if exam is not None:
            subjects.append(SubjectExamSlot(
                name=subj.name,
                exam_date=exam.scheduled_at.date().isoformat(),
                time_start=exam.scheduled_at.time().strftime("%H:%M"),
            ))
        else:
            subjects.append(SubjectExamSlot(
                name=subj.name,
                exam_date=None,
                time_start=None,
            ))

    return MySubjectsResponse(
        student_name=student.name,
        class_name=student.class_.name,
        data_confirmed=student.data_confirmed,
        subjects=subjects,
    )


# ---------------------------------------------------------------------------
# §9.2  POST /confirm/flag-error
# ---------------------------------------------------------------------------

@router.post("/flag-error")
def flag_error(
    body: FlagErrorRequest,
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    if not body.note or not body.note.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="note is required",
        )

    flag = DataFlag(student_id=student.id, note=body.note.strip())
    db.add(flag)

    # Spec §9.2: filing a flag UN-confirms the student. If they had
    # previously confirmed and now spotted a problem, the homeroom
    # teacher needs to see that they're back in 'unconfirmed' state.
    student.data_confirmed = False

    db.commit()
    return {"flag_id": str(flag.id), "data_confirmed": False}


# ---------------------------------------------------------------------------
# §9.3  POST /confirm/confirm
# ---------------------------------------------------------------------------

@router.post("/confirm")
def confirm(
    student: Student = Depends(get_current_student),
    db: Session = Depends(get_db),
):
    student.data_confirmed = True
    db.commit()
    return {"data_confirmed": True}


# ---------------------------------------------------------------------------
# §9.4  GET /confirm/homeroom-summary
# ---------------------------------------------------------------------------

@router.get("/homeroom-summary", response_model=HomeroomSummaryResponse)
def homeroom_summary(
    teacher: Teacher = Depends(get_current_teacher),
    db: Session = Depends(get_db),
):
    # Find the class this teacher is homeroom-of. A teacher can be
    # homeroom of at most one class (Class.homeroom_teacher_id is FK,
    # but there's no reverse uniqueness — defensive: take the first).
    cls = db.query(Class).filter_by(homeroom_teacher_id=teacher.id).first()
    if cls is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not assigned as homeroom teacher of any class",
        )

    # Subject count per student = count of ClassSubject rows for their class.
    # Same for every student in the class, so compute once.
    subject_count = (
        db.query(ClassSubject).filter_by(class_id=cls.id).count()
    )

    rows = []
    for s in cls.students:
        rows.append(HomeroomStudentRow(
            student_id=str(s.id),
            name=s.name,
            nisn=s.nisn,
            nis=s.nis,
            subject_count=subject_count,
            data_confirmed=s.data_confirmed,
            flagged=s.flagged,
            flag_reason=s.flag_reason,
        ))

    # Sort by name so the homeroom teacher can scan alphabetically.
    rows.sort(key=lambda r: r.name)

    return HomeroomSummaryResponse(
        class_name=cls.name,
        students=rows,
    )
