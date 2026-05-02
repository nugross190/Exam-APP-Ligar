"""Teacher portal router.

Spec §7. The teacher-side authoring surface for an exam:

  GET  /teacher/exams                              -> list my exams
  GET  /teacher/exam/{exam_id}/questions           -> list questions in one
  POST /teacher/exam/{exam_id}/question            -> create question + choices
  PUT  /teacher/question/{question_id}             -> replace question + choices
  POST /teacher/question/{question_id}/image       -> upload a jpg/png image

Ownership rule (spec §7.3): a teacher can only mutate questions whose
parent Exam belongs to a Subject they teach. We enforce this in
`_owned_exam` / `_owned_question` and treat any mismatch as 404 (don't
leak whether the exam/question exists at all).

Choice weighting (spec §7.2 + §1.4):
  correct_n = number of choices marked is_correct
  weight    = 1/correct_n if is_correct else 0.0
This makes the sum of correct weights equal to 1.0, which is what
exam.py:_score_question expects when it clamps complex_mc scores into
[0, 1] before multiplying by item_points.

Image upload: the spec calls for R2 in production; for local/test we
write into UPLOAD_DIR (default ./uploads) and store a relative URL of
the form /uploads/<filename>. main.py mounts that path with
StaticFiles, so the URL is fetchable end-to-end.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, HTTPException, UploadFile, status,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import Choice, Exam, Question, Subject, Teacher
from routers.auth import get_current_teacher

router = APIRouter(prefix="/teacher", tags=["teacher"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_QUESTION_TYPES = {"pg", "tf", "complex_mc"}
_ALLOWED_CHOICES_COUNTS = {4, 5}
_TF_CHOICES_COUNT = 2  # 'tf' is the documented exception (true/false)

_UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads")).resolve()
_UPLOAD_URL_PREFIX = "/uploads"
_ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB — generous for a question image


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ExamSummary(BaseModel):
    id: str
    title: str
    subject_name: str
    scheduled_at: datetime
    duration_minutes: int
    status: str
    admin_confirmed: bool
    question_count: int


class ChoiceIn(BaseModel):
    body: str
    is_correct: bool = False


class ChoiceOut(BaseModel):
    id: str
    body: str
    is_correct: bool
    weight: float


class QuestionIn(BaseModel):
    question_type: str = Field(..., description="'pg' | 'tf' | 'complex_mc'")
    body: str
    image_url: Optional[str] = None
    item_points: float = 1.0
    choices_count: int
    choices: list[ChoiceIn]


class QuestionOut(BaseModel):
    id: str
    exam_id: str
    question_type: str
    body: str
    image_url: Optional[str]
    item_points: float
    choices_count: int
    choices: list[ChoiceOut]


class CreateQuestionResponse(BaseModel):
    question_id: str


class ImageUploadResponse(BaseModel):
    question_id: str
    image_url: str


# ---------------------------------------------------------------------------
# Ownership helpers
# ---------------------------------------------------------------------------

def _owned_exam(db: Session, exam_id: str, teacher: Teacher) -> Exam:
    """Return the exam if the teacher owns its subject; else 404.

    We use 404 (not 403) deliberately — a teacher querying someone else's
    exam shouldn't even learn that exam exists.
    """
    exam = (
        db.query(Exam)
        .join(Subject, Exam.subject_id == Subject.id)
        .filter(Exam.id == exam_id, Subject.teacher_id == teacher.id)
        .first()
    )
    if exam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="exam not found")
    return exam


def _owned_question(db: Session, question_id: str, teacher: Teacher) -> Question:
    q = (
        db.query(Question)
        .join(Exam, Question.exam_id == Exam.id)
        .join(Subject, Exam.subject_id == Subject.id)
        .filter(Question.id == question_id, Subject.teacher_id == teacher.id)
        .first()
    )
    if q is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="question not found")
    return q


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_question_payload(payload: QuestionIn) -> tuple[int, list[ChoiceIn]]:
    """Return (correct_n, choices) after validating the payload.

    Rules (spec §7.2):
      - question_type in {'pg','tf','complex_mc'}
      - 'pg'/'complex_mc': choices_count in {4,5}; 'tf': choices_count == 2
      - choices list length must equal choices_count
      - at least 1 choice marked is_correct
      - 'pg'/'tf' must have exactly 1 correct choice (single-correct types)
      - item_points > 0
    """
    if payload.question_type not in _QUESTION_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"question_type must be one of {sorted(_QUESTION_TYPES)}",
        )

    if payload.question_type == "tf":
        if payload.choices_count != _TF_CHOICES_COUNT:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="tf questions must have choices_count=2",
            )
    else:
        if payload.choices_count not in _ALLOWED_CHOICES_COUNTS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"choices_count must be one of {sorted(_ALLOWED_CHOICES_COUNTS)}",
            )

    if len(payload.choices) != payload.choices_count:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"expected {payload.choices_count} choices, got {len(payload.choices)}",
        )

    if payload.item_points <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="item_points must be > 0",
        )

    correct_n = sum(1 for c in payload.choices if c.is_correct)
    if correct_n < 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="at least one choice must be marked is_correct",
        )
    if payload.question_type in {"pg", "tf"} and correct_n != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{payload.question_type} questions must have exactly one correct choice",
        )

    return correct_n, payload.choices


def _question_to_out(q: Question) -> QuestionOut:
    return QuestionOut(
        id=q.id,
        exam_id=q.exam_id,
        question_type=q.question_type,
        body=q.body,
        image_url=q.image_url,
        item_points=q.item_points,
        choices_count=q.choices_count,
        choices=[
            ChoiceOut(id=c.id, body=c.body, is_correct=c.is_correct, weight=c.weight)
            for c in q.choices
        ],
    )


# ---------------------------------------------------------------------------
# §7.1  GET /teacher/exams
# ---------------------------------------------------------------------------

@router.get("/exams", response_model=list[ExamSummary])
def list_my_exams(
    teacher: Teacher = Depends(get_current_teacher),
    db: Session = Depends(get_db),
):
    exams = (
        db.query(Exam)
        .join(Subject, Exam.subject_id == Subject.id)
        .filter(Subject.teacher_id == teacher.id)
        .order_by(Exam.scheduled_at)
        .all()
    )
    return [
        ExamSummary(
            id=e.id,
            title=e.title,
            subject_name=e.subject.name,
            scheduled_at=e.scheduled_at,
            duration_minutes=e.duration_minutes,
            status=e.status,
            admin_confirmed=e.admin_confirmed,
            question_count=len(e.questions),
        )
        for e in exams
    ]


# ---------------------------------------------------------------------------
#  GET /teacher/exam/{exam_id}/questions  (helper companion to §7.2)
# ---------------------------------------------------------------------------

@router.get("/exam/{exam_id}/questions", response_model=list[QuestionOut])
def list_exam_questions(
    exam_id: str,
    teacher: Teacher = Depends(get_current_teacher),
    db: Session = Depends(get_db),
):
    exam = _owned_exam(db, exam_id, teacher)
    return [_question_to_out(q) for q in exam.questions]


# ---------------------------------------------------------------------------
# §7.2  POST /teacher/exam/{exam_id}/question
# ---------------------------------------------------------------------------

@router.post(
    "/exam/{exam_id}/question",
    response_model=CreateQuestionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_question(
    exam_id: str,
    payload: QuestionIn,
    teacher: Teacher = Depends(get_current_teacher),
    db: Session = Depends(get_db),
):
    exam = _owned_exam(db, exam_id, teacher)
    if exam.admin_confirmed:
        # Once an admin opens the exam to students, the question set is
        # frozen — otherwise a teacher could swap questions mid-window.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="exam is admin_confirmed; questions are frozen",
        )

    correct_n, choices = _validate_question_payload(payload)

    q = Question(
        exam_id=exam.id,
        question_type=payload.question_type,
        body=payload.body,
        image_url=payload.image_url,
        item_points=payload.item_points,
        choices_count=payload.choices_count,
    )
    db.add(q)
    db.flush()  # need q.id for child rows

    for c in choices:
        db.add(Choice(
            question_id=q.id,
            body=c.body,
            is_correct=c.is_correct,
            weight=(1.0 / correct_n) if c.is_correct else 0.0,
        ))

    db.commit()
    return CreateQuestionResponse(question_id=q.id)


# ---------------------------------------------------------------------------
# §7.3  PUT /teacher/question/{question_id}
# ---------------------------------------------------------------------------

@router.put("/question/{question_id}", response_model=QuestionOut)
def update_question(
    question_id: str,
    payload: QuestionIn,
    teacher: Teacher = Depends(get_current_teacher),
    db: Session = Depends(get_db),
):
    q = _owned_question(db, question_id, teacher)
    if q.exam.admin_confirmed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="exam is admin_confirmed; questions are frozen",
        )

    correct_n, choices = _validate_question_payload(payload)

    q.question_type = payload.question_type
    q.body = payload.body
    q.image_url = payload.image_url
    q.item_points = payload.item_points
    q.choices_count = payload.choices_count

    # Replace choices wholesale — spec §7.3: "DELETE + recreate Choice rows".
    # The cascade on Question.choices makes this safe; we still flush so
    # the new rows aren't visible until commit.
    for old in list(q.choices):
        db.delete(old)
    db.flush()

    for c in choices:
        db.add(Choice(
            question_id=q.id,
            body=c.body,
            is_correct=c.is_correct,
            weight=(1.0 / correct_n) if c.is_correct else 0.0,
        ))

    db.commit()
    db.refresh(q)
    return _question_to_out(q)


# ---------------------------------------------------------------------------
# §7.4  POST /teacher/question/{question_id}/image
# ---------------------------------------------------------------------------

@router.post(
    "/question/{question_id}/image",
    response_model=ImageUploadResponse,
)
async def upload_question_image(
    question_id: str,
    file: UploadFile = File(...),
    teacher: Teacher = Depends(get_current_teacher),
    db: Session = Depends(get_db),
):
    q = _owned_question(db, question_id, teacher)
    if q.exam.admin_confirmed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="exam is admin_confirmed; questions are frozen",
        )

    ext = _ALLOWED_IMAGE_TYPES.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported content_type {file.content_type!r}; want jpg or png",
        )

    # Read with a hard cap so a malicious client can't fill the disk.
    data = await file.read(_MAX_IMAGE_BYTES + 1)
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {_MAX_IMAGE_BYTES} bytes",
        )
    if not data:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="empty file",
        )

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{q.id}_{uuid.uuid4().hex}{ext}"
    dest = _UPLOAD_DIR / filename
    dest.write_bytes(data)

    q.image_url = f"{_UPLOAD_URL_PREFIX}/{filename}"
    db.commit()
    return ImageUploadResponse(question_id=q.id, image_url=q.image_url)
