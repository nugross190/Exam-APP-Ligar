"""Exam engine router - handles exam sessions, questions, and scoring."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
import jwt
from pydantic import BaseModel, Field

import sys
sys.path.append('/workspace')
from database import get_db
from models import Student, Exam, Question, Choice as Answer, ExamSession, SessionViolation as Violation
from routers.auth import decode_jwt

router = APIRouter(prefix="/api/exam", tags=["exam"])


# --- Pydantic Schemas ---

class ExamListItem(BaseModel):
    id: int
    title: str
    subject: str
    duration_minutes: int
    total_questions: int
    starts_at: datetime
    ends_at: datetime
    status: str  # upcoming, active, completed

    class Config:
        from_attributes = True


class QuestionItem(BaseModel):
    id: int
    exam_question_id: int
    question_number: int
    text: str
    question_type: str
    options: Optional[List[str]] = None
    image_url: Optional[str] = None

    class Config:
        from_attributes = True


class AnswerSubmit(BaseModel):
    exam_question_id: int
    answer_text: str


class AnswerSubmitResponse(BaseModel):
    success: bool
    message: str


class ExamSessionStart(BaseModel):
    exam_id: int


class ExamSessionInfo(BaseModel):
    session_id: int
    exam_id: int
    exam_title: str
    time_remaining_seconds: int
    questions_answered: int
    total_questions: int
    status: str

    class Config:
        from_attributes = True


class ExamSubmissionResult(BaseModel):
    success: bool
    score: float
    percentage: float
    correct_count: int
    total_count: int
    message: str


# --- Helper Functions ---

def get_current_student(db: Session = Depends(get_db), token: str = Depends(decode_jwt)) -> Student:
    """Extract current student from JWT token."""
    try:
        # decode_jwt returns dict with 'sub' and 'role'
        student_id = token.get("sub")
        if student_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        
        student = db.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")
        return student
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# --- Routes ---

@router.get("/available", response_model=List[ExamListItem])
def list_available_exams(
    current_student: Student = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    List all exams available to the current student.
    Returns exams with status: upcoming, active, or completed.
    """
    now = datetime.utcnow()
    
    # Get all exams this student is enrolled in via their class level
    exams = db.query(Exam).filter(
        Exam.class_level == current_student.class_level,
        Exam.subject == current_student.stream
    ).order_by(Exam.starts_at).all()
    
    result = []
    for exam in exams:
        # Determine status
        if now < exam.starts_at:
            status_val = "upcoming"
        elif now > exam.ends_at:
            status_val = "completed"
        else:
            status_val = "active"
        
        # Count total questions
        total_q = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).count()
        
        result.append(ExamListItem(
            id=exam.id,
            title=exam.title,
            subject=exam.subject,
            duration_minutes=exam.duration_minutes,
            total_questions=total_q,
            starts_at=exam.starts_at,
            ends_at=exam.ends_at,
            status=status_val
        ))
    
    return result


@router.post("/session/start", response_model=ExamSessionInfo)
def start_exam_session(
    session_data: ExamSessionStart,
    current_student: Student = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    Start a new exam session for the student.
    Creates an ExamSession record and returns session info.
    """
    exam = db.query(Exam).filter(Exam.id == session_data.exam_id).first()
    if not exam:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam not found")
    
    now = datetime.utcnow()
    
    # Validate exam timing
    if now < exam.starts_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Exam has not started yet. Starts at {exam.starts_at}"
        )
    
    if now > exam.ends_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exam has already ended"
        )
    
    # Check if student already has an active session for this exam
    existing_session = db.query(ExamSession).filter(
        ExamSession.student_id == current_student.id,
        ExamSession.exam_id == exam.id,
        ExamSession.status == "in_progress"
    ).first()
    
    if existing_session:
        # Return existing session
        total_q = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).count()
        answered = db.query(Answer).filter(
            Answer.session_id == existing_session.id
        ).count()
        
        time_remaining = (exam.ends_at - now).total_seconds()
        
        return ExamSessionInfo(
            session_id=existing_session.id,
            exam_id=exam.id,
            exam_title=exam.title,
            time_remaining_seconds=int(time_remaining),
            questions_answered=answered,
            total_questions=total_q,
            status=existing_session.status
        )
    
    # Create new session
    new_session = ExamSession(
        student_id=current_student.id,
        exam_id=exam.id,
        started_at=now,
        status="in_progress"
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    
    total_q = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).count()
    time_remaining = (exam.ends_at - now).total_seconds()
    
    return ExamSessionInfo(
        session_id=new_session.id,
        exam_id=exam.id,
        exam_title=exam.title,
        time_remaining_seconds=int(time_remaining),
        questions_answered=0,
        total_questions=total_q,
        status="in_progress"
    )


@router.get("/session/{session_id}/question", response_model=QuestionItem)
def get_question(
    session_id: int,
    exam_question_id: int,
    current_student: Student = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    Get a specific question for an exam session.
    Returns question WITHOUT the correct answer.
    """
    # Verify session belongs to student
    session = db.query(ExamSession).filter(
        ExamSession.id == session_id,
        ExamSession.student_id == current_student.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    if session.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exam session is not active"
        )
    
    # Get the exam question
    exam_question = db.query(ExamQuestion).filter(
        ExamQuestion.id == exam_question_id,
        ExamQuestion.exam_id == session.exam_id
    ).first()
    
    if not exam_question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    
    question = exam_question.question
    
    # Build options list for multiple choice
    options = None
    if question.question_type == "multiple_choice":
        options = [
            question.option_a,
            question.option_b,
            question.option_c,
            question.option_d
        ]
        # Filter out None values
        options = [opt for opt in options if opt is not None]
    
    return QuestionItem(
        id=question.id,
        exam_question_id=exam_question.id,
        question_number=exam_question.question_number,
        text=question.text,
        question_type=question.question_type,
        options=options,
        image_url=question.image_url
    )


@router.post("/session/{session_id}/answer", response_model=AnswerSubmitResponse)
def submit_answer(
    session_id: int,
    answer_data: AnswerSubmit,
    current_student: Student = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    Submit an answer for a question in the current exam session.
    Students can re-submit answers before final exam submission.
    """
    # Verify session belongs to student
    session = db.query(ExamSession).filter(
        ExamSession.id == session_id,
        ExamSession.student_id == current_student.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    if session.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exam session is not active"
        )
    
    # Verify exam_question belongs to this exam
    exam_question = db.query(ExamQuestion).filter(
        ExamQuestion.id == answer_data.exam_question_id,
        ExamQuestion.exam_id == session.exam_id
    ).first()
    
    if not exam_question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found in this exam")
    
    # Check if answer already exists (allow update)
    existing_answer = db.query(Answer).filter(
        Answer.session_id == session_id,
        Answer.exam_question_id == answer_data.exam_question_id
    ).first()
    
    if existing_answer:
        existing_answer.answer_text = answer_data.answer_text
        existing_answer.updated_at = datetime.utcnow()
    else:
        new_answer = Answer(
            session_id=session_id,
            exam_question_id=answer_data.exam_question_id,
            answer_text=answer_data.answer_text
        )
        db.add(new_answer)
    
    db.commit()
    
    return AnswerSubmitResponse(
        success=True,
        message="Answer saved successfully"
    )


@router.post("/session/{session_id}/submit", response_model=ExamSubmissionResult)
def submit_exam(
    session_id: int,
    current_student: Student = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    Submit the exam for grading.
    Calculates score immediately and updates session status.
    """
    # Verify session belongs to student
    session = db.query(ExamSession).filter(
        ExamSession.id == session_id,
        ExamSession.student_id == current_student.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    if session.status == "submitted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exam has already been submitted"
        )
    
    # Get all exam questions
    exam_questions = db.query(ExamQuestion).filter(
        ExamQuestion.exam_id == session.exam_id
    ).all()
    
    total_count = len(exam_questions)
    correct_count = 0
    
    # Grade each question
    for eq in exam_questions:
        # Find student's answer
        answer = db.query(Answer).filter(
            Answer.session_id == session_id,
            Answer.exam_question_id == eq.id
        ).first()
        
        student_answer = answer.answer_text.strip().lower() if answer and answer.answer_text else ""
        correct_answer = eq.question.correct_answer.strip().lower() if eq.question.correct_answer else ""
        
        # Simple string comparison for grading
        if student_answer == correct_answer:
            correct_count += 1
        
        # Store the correct answer in the Answer record for review
        if answer:
            answer.is_correct = (student_answer == correct_answer)
            answer.points_earned = eq.points if answer.is_correct else 0
    
    # Calculate score
    score = correct_count / total_count if total_count > 0 else 0
    percentage = score * 100
    
    # Update session
    session.status = "submitted"
    session.submitted_at = datetime.utcnow()
    session.score = score
    
    db.commit()
    
    return ExamSubmissionResult(
        success=True,
        score=score,
        percentage=percentage,
        correct_count=correct_count,
        total_count=total_count,
        message=f"Exam submitted! Score: {correct_count}/{total_count} ({percentage:.1f}%)"
    )


@router.get("/session/{session_id}/status", response_model=ExamSessionInfo)
def get_session_status(
    session_id: int,
    current_student: Student = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """Get current status of an exam session."""
    session = db.query(ExamSession).filter(
        ExamSession.id == session_id,
        ExamSession.student_id == current_student.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    exam = db.query(Exam).filter(Exam.id == session.exam_id).first()
    total_q = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).count()
    answered = db.query(Answer).filter(Answer.session_id == session_id).count()
    
    now = datetime.utcnow()
    if session.status == "in_progress":
        time_remaining = max(0, (exam.ends_at - now).total_seconds())
    else:
        time_remaining = 0
    
    return ExamSessionInfo(
        session_id=session.id,
        exam_id=exam.id,
        exam_title=exam.title,
        time_remaining_seconds=int(time_remaining),
        questions_answered=answered,
        total_questions=total_q,
        status=session.status
    )
