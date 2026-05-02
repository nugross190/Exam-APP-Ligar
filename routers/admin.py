"""Admin panel router.

Spec §8. The admin/owner control surface for an exam:

  POST /admin/exam/{exam_id}/confirm    -> open the exam to students
  GET  /admin/exam/{exam_id}/monitor    -> live status counts + flags
  GET  /admin/results/{exam_id}/export  -> xlsx of per-student / per-class
  POST /admin/import/students           -> multipart xlsx upload + seed
  POST /admin/import/schedule           -> multipart csv upload + seed

All endpoints are gated by `require_role('admin','owner')`. Imports
support `?dry_run=true` so the operator can preview parser warnings and
counts before mutating any rows — same behaviour as the seed CLI's
--dry-run flag.

Pass-rate (sheet 2 of the export): defined as the share of students
whose `total_score / max_score` is >= PASSING_PERCENT/100. The
constant lives in this module so it's the one place to change if the
school's KKM moves.
"""
from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import (
    Class, Exam, ExamResult, ExamSession, ExpelledFlag,
    SessionViolation, Student,
)
from parsers.excel import (
    derive_class_subjects, parse_schedule, parse_students,
)
from routers.auth import require_role
from seed import (
    seed_class_subjects, seed_classes_and_students, seed_subjects_and_exams,
)

router = APIRouter(prefix="/admin", tags=["admin"])

PASSING_PERCENT = 70.0  # KKM. Sheet 2 pass_rate uses this threshold.


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ConfirmExamResponse(BaseModel):
    exam_id: str
    admin_confirmed: bool
    status: str
    confirmed_at: datetime


class MonitorCounts(BaseModel):
    total: int
    pending: int
    active: int
    submitted: int
    expelled: int
    panic: int


class ViolationRow(BaseModel):
    session_id: str
    student_id: str
    student_name: str
    class_name: str
    violation_count: int
    status: str


class HomeroomFlagRow(BaseModel):
    session_id: str
    student_id: str
    student_name: str
    class_name: str
    homeroom_teacher_id: Optional[str]
    expelled_at: Optional[datetime]
    acknowledged: bool


class MonitorResponse(BaseModel):
    exam_id: str
    counts: MonitorCounts
    violations: list[ViolationRow]
    homeroom_flags: list[HomeroomFlagRow]


class ImportStudentsResponse(BaseModel):
    dry_run: bool
    parsed_rows: int
    flagged_rows: int
    warnings: list[str]
    seed_stats: Optional[dict] = None


class ImportScheduleResponse(BaseModel):
    dry_run: bool
    parsed_entries: int
    classes_touched: int
    warnings: list[str]
    seed_stats: Optional[dict] = None


# ---------------------------------------------------------------------------
# §8.1  POST /admin/exam/{exam_id}/confirm
# ---------------------------------------------------------------------------

@router.post(
    "/exam/{exam_id}/confirm",
    response_model=ConfirmExamResponse,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def confirm_exam(exam_id: str, db: Session = Depends(get_db)):
    exam = db.query(Exam).filter_by(id=exam_id).first()
    if exam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="exam not found")

    exam.admin_confirmed = True
    now = datetime.utcnow()
    # Spec §8.1: 'open' if we're already past scheduled_at (admin is
    # confirming late, students should be able to start immediately);
    # otherwise 'scheduled' and start_exam will gate on the time window.
    exam.status = "open" if now >= exam.scheduled_at else "scheduled"
    db.commit()
    db.refresh(exam)

    return ConfirmExamResponse(
        exam_id=exam.id,
        admin_confirmed=exam.admin_confirmed,
        status=exam.status,
        confirmed_at=now,
    )


# ---------------------------------------------------------------------------
# §8.2  GET /admin/exam/{exam_id}/monitor
# ---------------------------------------------------------------------------

@router.get(
    "/exam/{exam_id}/monitor",
    response_model=MonitorResponse,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def monitor_exam(exam_id: str, db: Session = Depends(get_db)):
    exam = db.query(Exam).filter_by(id=exam_id).first()
    if exam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="exam not found")

    sessions = (
        db.query(ExamSession)
        .filter_by(exam_id=exam.id)
        .all()
    )
    counts = MonitorCounts(
        total=len(sessions),
        pending=sum(1 for s in sessions if s.status == "pending"),
        active=sum(1 for s in sessions if s.status == "active"),
        submitted=sum(1 for s in sessions if s.status == "submitted"),
        expelled=sum(1 for s in sessions if s.status == "expelled"),
        panic=sum(1 for s in sessions if s.status == "panic"),
    )

    violations: list[ViolationRow] = []
    homeroom_flags: list[HomeroomFlagRow] = []

    # Pre-fetch ExpelledFlag rows for this exam's sessions so we can show
    # acknowledgement state without an N+1 query.
    flags_by_session = {
        f.session_id: f
        for f in db.query(ExpelledFlag).filter(
            ExpelledFlag.session_id.in_([s.id for s in sessions] or [""]),
        ).all()
    }

    for s in sessions:
        if s.violation_count and s.violation_count > 0:
            violations.append(ViolationRow(
                session_id=s.id,
                student_id=s.student_id,
                student_name=s.student.name,
                class_name=s.student.class_.name,
                violation_count=s.violation_count,
                status=s.status,
            ))
        if s.status == "expelled":
            f = flags_by_session.get(s.id)
            homeroom_flags.append(HomeroomFlagRow(
                session_id=s.id,
                student_id=s.student_id,
                student_name=s.student.name,
                class_name=s.student.class_.name,
                homeroom_teacher_id=s.student.class_.homeroom_teacher_id,
                expelled_at=s.submitted_at,
                acknowledged=bool(f and f.acknowledged_at),
            ))

    return MonitorResponse(
        exam_id=exam.id,
        counts=counts,
        violations=violations,
        homeroom_flags=homeroom_flags,
    )


# ---------------------------------------------------------------------------
# §8.3  GET /admin/results/{exam_id}/export
# ---------------------------------------------------------------------------

@router.get(
    "/results/{exam_id}/export",
    dependencies=[Depends(require_role("admin", "owner"))],
)
def export_results(exam_id: str, db: Session = Depends(get_db)):
    exam = db.query(Exam).filter_by(id=exam_id).first()
    if exam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="exam not found")

    rows = (
        db.query(ExamResult, ExamSession, Student, Class)
        .join(ExamSession, ExamResult.session_id == ExamSession.id)
        .join(Student, ExamSession.student_id == Student.id)
        .join(Class, Student.class_id == Class.id)
        .filter(ExamSession.exam_id == exam.id)
        .all()
    )

    wb = Workbook()
    # ----- Sheet 1: per student -----
    ws1 = wb.active
    ws1.title = "Per Student"
    ws1.append([
        "NIS", "Name", "Class", "Score", "Max Score",
        "Percentage", "Finalized At",
    ])

    # Aggregate for sheet 2 while iterating once.
    by_class: dict[str, list[float]] = {}
    for result, _session, student, klass in rows:
        pct = (
            (result.total_score / result.max_score * 100.0)
            if result.max_score > 0 else 0.0
        )
        ws1.append([
            student.nis,
            student.name,
            klass.name,
            round(result.total_score, 2),
            round(result.max_score, 2),
            round(pct, 2),
            result.finalized_at.isoformat() if result.finalized_at else "",
        ])
        by_class.setdefault(klass.name, []).append(pct)

    # ----- Sheet 2: per class -----
    ws2 = wb.create_sheet("Per Class")
    ws2.append([
        "Class", "Students", "Avg %", "Min %", "Max %",
        f"Pass Rate (>= {PASSING_PERCENT:g}%)",
    ])
    for class_name in sorted(by_class):
        pcts = by_class[class_name]
        n = len(pcts)
        passes = sum(1 for p in pcts if p >= PASSING_PERCENT)
        ws2.append([
            class_name,
            n,
            round(sum(pcts) / n, 2) if n else 0,
            round(min(pcts), 2) if n else 0,
            round(max(pcts), 2) if n else 0,
            round(passes / n * 100.0, 2) if n else 0,
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_title = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in (exam.title or "exam")
    )
    filename = f"results_{safe_title}_{exam.id[:8]}.xlsx"
    return StreamingResponse(
        buf,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# §8.4  POST /admin/import/students
# ---------------------------------------------------------------------------

def _save_upload_to_tmp(upload: UploadFile, suffix: str) -> str:
    """Persist an UploadFile to a NamedTemporaryFile and return its path.

    The parsers in parsers/excel.py only accept paths (openpyxl needs to
    seek), so we have to materialize the upload to disk. Caller is
    responsible for os.unlink-ing the path.
    """
    contents = upload.file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(contents)
    finally:
        tmp.close()
    return tmp.name


@router.post(
    "/import/students",
    response_model=ImportStudentsResponse,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def import_students(
    xi_file: UploadFile = File(..., description="Grade XI roster .xlsx"),
    x_file: UploadFile = File(..., description="Grade X roster .xlsx"),
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
):
    xi_path = _save_upload_to_tmp(xi_file, ".xlsx")
    x_path = _save_upload_to_tmp(x_file, ".xlsx")
    try:
        parsed = parse_students(xi_path, x_path)
    finally:
        for p in (xi_path, x_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    flagged = sum(1 for r in parsed.data if r.flags)

    if dry_run:
        return ImportStudentsResponse(
            dry_run=True,
            parsed_rows=len(parsed.data),
            flagged_rows=flagged,
            warnings=parsed.warnings,
        )

    try:
        seed_stats = seed_classes_and_students(parsed.data, db)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return ImportStudentsResponse(
        dry_run=False,
        parsed_rows=len(parsed.data),
        flagged_rows=flagged,
        warnings=parsed.warnings,
        seed_stats=seed_stats,
    )


# ---------------------------------------------------------------------------
# §8.5  POST /admin/import/schedule
# ---------------------------------------------------------------------------

@router.post(
    "/import/schedule",
    response_model=ImportScheduleResponse,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def import_schedule(
    schedule_file: UploadFile = File(..., description="Pre-parsed schedule .csv"),
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
):
    sched_path = _save_upload_to_tmp(schedule_file, ".csv")
    try:
        parsed = parse_schedule(sched_path)
    finally:
        try:
            os.unlink(sched_path)
        except OSError:
            pass

    class_subjects = derive_class_subjects(parsed.data)

    if dry_run:
        return ImportScheduleResponse(
            dry_run=True,
            parsed_entries=len(parsed.data),
            classes_touched=len(class_subjects),
            warnings=parsed.warnings,
        )

    try:
        s_exam = seed_subjects_and_exams(parsed.data, db)
        s_link = seed_class_subjects(class_subjects, db)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return ImportScheduleResponse(
        dry_run=False,
        parsed_entries=len(parsed.data),
        classes_touched=len(class_subjects),
        warnings=parsed.warnings,
        seed_stats={**s_exam, **s_link},
    )
