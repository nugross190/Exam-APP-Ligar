"""SQLAlchemy models for HADIR Exam App.

Implements spec §1 (13 models) plus 2 models referenced but not defined:
  - DataFlag      (referenced in §9.2 POST /confirm/flag-error)
  - ExpelledFlag  (referenced in §6.2 violation handler)

UUID primary keys throughout. Postgres in production, SQLite for tests
(both supported via the String(36) UUID column type).
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON,
    String, Text, Time, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

from database import utcnow

Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# §1.1  Identity
# ---------------------------------------------------------------------------

class Class(Base):
    __tablename__ = "classes"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(20), unique=True, nullable=False, index=True)  # 'XI - A'
    grade = Column(String(2), nullable=False)                            # 'X' or 'XI'
    homeroom_teacher_id = Column(String(36), ForeignKey("teachers.id"), nullable=True)

    students = relationship("Student", back_populates="class_", cascade="all, delete-orphan")
    class_subjects = relationship("ClassSubject", back_populates="class_", cascade="all, delete-orphan")
    homeroom_teacher = relationship("Teacher", foreign_keys=[homeroom_teacher_id])


class Teacher(Base):
    __tablename__ = "teachers"
    id = Column(String(36), primary_key=True, default=_uuid)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(20), nullable=False)  # 'teacher'|'admin'|'owner'|'homeroom'

    subjects = relationship("Subject", back_populates="teacher")


class Student(Base):
    __tablename__ = "students"
    id = Column(String(36), primary_key=True, default=_uuid)
    nisn = Column(String(20), nullable=False, index=True)
    nis = Column(String(30), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    gender = Column(String(1), nullable=False)
    class_id = Column(String(36), ForeignKey("classes.id"), nullable=False)
    username = Column(String(30), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    data_confirmed = Column(Boolean, nullable=False, default=False)
    flagged = Column(Boolean, nullable=False, default=False)
    flag_reason = Column(String(100), nullable=True)

    class_ = relationship("Class", back_populates="students")
    sessions = relationship("ExamSession", back_populates="student", cascade="all, delete-orphan")
    data_flags = relationship("DataFlag", back_populates="student", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# §1.2  Curriculum
# ---------------------------------------------------------------------------

class Subject(Base):
    __tablename__ = "subjects"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(100), unique=True, nullable=False, index=True)
    teacher_id = Column(String(36), ForeignKey("teachers.id"), nullable=True)

    teacher = relationship("Teacher", back_populates="subjects")
    exams = relationship("Exam", back_populates="subject")
    class_subjects = relationship("ClassSubject", back_populates="subject")


class ClassSubject(Base):
    __tablename__ = "class_subjects"
    __table_args__ = (UniqueConstraint("class_id", "subject_id", name="uq_class_subject"),)
    id = Column(String(36), primary_key=True, default=_uuid)
    class_id = Column(String(36), ForeignKey("classes.id"), nullable=False)
    subject_id = Column(String(36), ForeignKey("subjects.id"), nullable=False)

    class_ = relationship("Class", back_populates="class_subjects")
    subject = relationship("Subject", back_populates="class_subjects")


# ---------------------------------------------------------------------------
# §1.3  Exam scheduling
# ---------------------------------------------------------------------------

class Exam(Base):
    __tablename__ = "exams"
    id = Column(String(36), primary_key=True, default=_uuid)
    subject_id = Column(String(36), ForeignKey("subjects.id"), nullable=False)
    title = Column(String(150), nullable=False)
    duration_minutes = Column(Integer, nullable=False, default=90)
    scheduled_at = Column(DateTime, nullable=False)
    time_end = Column(Time, nullable=False)
    status = Column(String(20), nullable=False, default="scheduled")  # draft|scheduled|open|closed
    admin_confirmed = Column(Boolean, nullable=False, default=False)

    subject = relationship("Subject", back_populates="exams")
    questions = relationship("Question", back_populates="exam", cascade="all, delete-orphan")
    sessions = relationship("ExamSession", back_populates="exam", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# §1.4  Question bank
# ---------------------------------------------------------------------------

class Question(Base):
    __tablename__ = "questions"
    id = Column(String(36), primary_key=True, default=_uuid)
    exam_id = Column(String(36), ForeignKey("exams.id"), nullable=False)
    question_type = Column(String(20), nullable=False)  # 'pg'|'tf'|'complex_mc'
    body = Column(Text, nullable=False)
    image_url = Column(String(500), nullable=True)
    item_points = Column(Float, nullable=False, default=1.0)
    choices_count = Column(Integer, nullable=False)  # 4 or 5

    exam = relationship("Exam", back_populates="questions")
    choices = relationship("Choice", back_populates="question", cascade="all, delete-orphan")


class Choice(Base):
    __tablename__ = "choices"
    id = Column(String(36), primary_key=True, default=_uuid)
    question_id = Column(String(36), ForeignKey("questions.id"), nullable=False)
    body = Column(Text, nullable=False)
    is_correct = Column(Boolean, nullable=False, default=False)
    weight = Column(Float, nullable=False, default=0.0)  # 1/choices_count if correct else 0

    question = relationship("Question", back_populates="choices")


# ---------------------------------------------------------------------------
# §1.5  Exam session
# ---------------------------------------------------------------------------

class ExamSession(Base):
    __tablename__ = "exam_sessions"
    id = Column(String(36), primary_key=True, default=_uuid)
    student_id = Column(String(36), ForeignKey("students.id"), nullable=False)
    exam_id = Column(String(36), ForeignKey("exams.id"), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    # ^ 'pending'|'active'|'submitted'|'expelled'|'panic'
    violation_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    panic_at = Column(DateTime, nullable=True)
    question_order = Column(JSON, nullable=True)  # [question_id, ...]

    student = relationship("Student", back_populates="sessions")
    exam = relationship("Exam", back_populates="sessions")
    violations = relationship("SessionViolation", back_populates="session", cascade="all, delete-orphan")
    answers = relationship("StudentAnswer", back_populates="session", cascade="all, delete-orphan")
    result = relationship("ExamResult", back_populates="session", uselist=False, cascade="all, delete-orphan")


class SessionViolation(Base):
    __tablename__ = "session_violations"
    id = Column(String(36), primary_key=True, default=_uuid)
    session_id = Column(String(36), ForeignKey("exam_sessions.id"), nullable=False)
    event_type = Column(String(30), nullable=False)  # 'tab_switch'|'fullscreen_exit'
    occurred_at = Column(DateTime, nullable=False, default=utcnow)

    session = relationship("ExamSession", back_populates="violations")


class StudentAnswer(Base):
    __tablename__ = "student_answers"
    __table_args__ = (UniqueConstraint("session_id", "question_id", name="uq_session_question"),)
    id = Column(String(36), primary_key=True, default=_uuid)
    session_id = Column(String(36), ForeignKey("exam_sessions.id"), nullable=False)
    question_id = Column(String(36), ForeignKey("questions.id"), nullable=False)
    score_earned = Column(Float, nullable=True)

    session = relationship("ExamSession", back_populates="answers")
    answer_choices = relationship("AnswerChoice", back_populates="student_answer", cascade="all, delete-orphan")


class AnswerChoice(Base):
    __tablename__ = "answer_choices"
    id = Column(String(36), primary_key=True, default=_uuid)
    student_answer_id = Column(String(36), ForeignKey("student_answers.id"), nullable=False)
    choice_id = Column(String(36), ForeignKey("choices.id"), nullable=False)

    student_answer = relationship("StudentAnswer", back_populates="answer_choices")


class ExamResult(Base):
    __tablename__ = "exam_results"
    id = Column(String(36), primary_key=True, default=_uuid)
    session_id = Column(String(36), ForeignKey("exam_sessions.id"), unique=True, nullable=False)
    total_score = Column(Float, nullable=False)
    max_score = Column(Float, nullable=False)
    finalized_at = Column(DateTime, nullable=False, default=utcnow)

    session = relationship("ExamSession", back_populates="result")


# ---------------------------------------------------------------------------
# Models referenced in spec but not defined in §1
# ---------------------------------------------------------------------------

class DataFlag(Base):
    """Created by POST /confirm/flag-error (§9.2). Lets students flag
    incorrect subject lists during the data confirmation week."""
    __tablename__ = "data_flags"
    id = Column(String(36), primary_key=True, default=_uuid)
    student_id = Column(String(36), ForeignKey("students.id"), nullable=False)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    student = relationship("Student", back_populates="data_flags")


class ExpelledFlag(Base):
    """Created by POST /violation when violation_count >= 3 (§6.2). Surfaced
    to the homeroom teacher of the expelled student's class."""
    __tablename__ = "expelled_flags"
    id = Column(String(36), primary_key=True, default=_uuid)
    session_id = Column(String(36), ForeignKey("exam_sessions.id"), nullable=False)
    class_id = Column(String(36), ForeignKey("classes.id"), nullable=False)
    student_id = Column(String(36), ForeignKey("students.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    acknowledged_at = Column(DateTime, nullable=True)
