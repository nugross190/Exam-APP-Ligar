"""Violation tracking router - lockdown browser enforcement."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

import sys
sys.path.append('/workspace')
from database import get_db
from models import Student, ExamSession, SessionViolation as Violation
from routers.auth import decode_jwt
import jwt

router = APIRouter(prefix="/api/violation", tags=["violation"])


# --- Pydantic Schemas ---

class ViolationReport(BaseModel):
    session_id: int
    violation_type: str  # tab_switch, fullscreen_exit, copy_paste, right_click, dev_tools
    description: Optional[str] = None


class ViolationResponse(BaseModel):
    success: bool
    warning_level: int  # 0-3
    lockout_remaining_seconds: Optional[int] = None
    message: str


class PanicButtonRequest(BaseModel):
    session_id: int
    exit_code: str  # reason code for emergency exit


class PanicButtonResponse(BaseModel):
    success: bool
    session_terminated: bool
    message: str


# --- Helper Functions ---

def get_current_student(token: str = Depends(decode_jwt)) -> dict:
    """Extract current student from JWT token."""
    try:
        # decode_jwt returns dict with 'sub' and 'role'
        student_id = token.get("sub")
        if student_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        
        return {"id": student_id}
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# --- Routes ---

@router.post("/report", response_model=ViolationResponse)
def report_violation(
    violation_data: ViolationReport,
    token_payload: dict = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    Report a lockdown browser violation.
    Implements progressive lockout system:
    - 1st violation: Warning
    - 2nd violation: 5-minute lockout
    - 3rd violation: 15-minute lockout  
    - 4th+ violation: Expelled from exam
    """
    student_id = token_payload["id"]
    
    # Verify session belongs to student
    session = db.query(ExamSession).filter(
        ExamSession.id == violation_data.session_id,
        ExamSession.student_id == student_id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    if session.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot report violations for inactive sessions"
        )
    
    # Record the violation
    new_violation = Violation(
        session_id=violation_data.session_id,
        violation_type=violation_data.violation_type,
        description=violation_data.description,
        severity="medium" if violation_data.violation_type in ["tab_switch", "fullscreen_exit"] else "low",
        resolved=False
    )
    db.add(new_violation)
    
    # Count violations for this session
    violation_count = db.query(Violation).filter(
        Violation.session_id == violation_data.session_id
    ).count()
    
    # Determine consequence based on violation count
    warning_level = min(violation_count, 4)
    lockout_remaining = None
    message = ""
    
    if violation_count == 1:
        message = "Warning: First violation recorded. Further violations will result in lockouts."
    elif violation_count == 2:
        lockout_until = datetime.utcnow() + timedelta(minutes=5)
        session.lockout_until = lockout_until
        lockout_remaining = 300  # 5 minutes in seconds
        message = "Lockout imposed: 5 minutes. You cannot continue until the lockout expires."
    elif violation_count == 3:
        lockout_until = datetime.utcnow() + timedelta(minutes=15)
        session.lockout_until = lockout_until
        lockout_remaining = 900  # 15 minutes in seconds
        message = "Severe lockout: 15 minutes. One more violation will expel you from the exam."
    elif violation_count >= 4:
        session.status = "expelled"
        session.expelled_at = datetime.utcnow()
        message = "EXPULSION: You have been expelled from the exam due to repeated violations."
    
    db.commit()
    
    return ViolationResponse(
        success=True,
        warning_level=warning_level,
        lockout_remaining_seconds=lockout_remaining,
        message=message
    )


@router.post("/panic", response_model=PanicButtonResponse)
def panic_button(
    panic_data: PanicButtonRequest,
    token_payload: dict = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    Emergency exit button for students.
    Records exit code and terminates session gracefully.
    Used for technical issues or emergencies.
    """
    student_id = token_payload["id"]
    
    # Verify session belongs to student
    session = db.query(ExamSession).filter(
        ExamSession.id == panic_data.session_id,
        ExamSession.student_id == student_id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    if session.status != "in_progress":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session is not active"
        )
    
    # Record violation with special exit code
    violation = Violation(
        session_id=panic_data.session_id,
        violation_type="panic_exit",
        description=f"Emergency exit with code: {panic_data.exit_code}",
        severity="info",
        resolved=True
    )
    db.add(violation)
    
    # Terminate session
    session.status = "submitted"
    session.submitted_at = datetime.utcnow()
    # Note: Score will be 0 or calculated based on answered questions
    
    db.commit()
    
    return PanicButtonResponse(
        success=True,
        session_terminated=True,
        message=f"Emergency exit recorded (code: {panic_data.exit_code}). Session terminated."
    )


@router.get("/session/{session_id}/lockout-status")
def get_lockout_status(
    session_id: int,
    token_payload: dict = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """
    Check if student is currently in lockout period.
    Returns remaining lockout time if applicable.
    """
    student_id = token_payload["id"]
    
    session = db.query(ExamSession).filter(
        ExamSession.id == session_id,
        ExamSession.student_id == student_id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    # Check if expelled
    if session.status == "expelled":
        return {
            "is_locked_out": True,
            "is_expelled": True,
            "lockout_remaining_seconds": None,
            "message": "You have been expelled from this exam."
        }
    
    # Check if in lockout period
    if session.lockout_until and session.lockout_until > datetime.utcnow():
        remaining = (session.lockout_until - datetime.utcnow()).total_seconds()
        return {
            "is_locked_out": True,
            "is_expelled": False,
            "lockout_remaining_seconds": int(remaining),
            "message": f"You are in lockout. Remaining time: {int(remaining)} seconds."
        }
    
    # Clear expired lockout
    if session.lockout_until and session.lockout_until <= datetime.utcnow():
        session.lockout_until = None
        db.commit()
    
    return {
        "is_locked_out": False,
        "is_expelled": False,
        "lockout_remaining_seconds": 0,
        "message": "No active lockout. You may continue the exam."
    }


@router.get("/session/{session_id}/history")
def get_violation_history(
    session_id: int,
    token_payload: dict = Depends(get_current_student),
    db: Session = Depends(get_db)
):
    """Get violation history for a session (for admin/teacher review)."""
    student_id = token_payload["id"]
    
    session = db.query(ExamSession).filter(
        ExamSession.id == session_id,
        ExamSession.student_id == student_id
    ).first()
    
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    
    violations = db.query(Violation).filter(
        Violation.session_id == session_id
    ).order_by(Violation.created_at).all()
    
    return {
        "session_id": session_id,
        "total_violations": len(violations),
        "violations": [
            {
                "id": v.id,
                "type": v.violation_type,
                "description": v.description,
                "severity": v.severity,
                "timestamp": v.created_at,
                "resolved": v.resolved
            }
            for v in violations
        ]
    }
