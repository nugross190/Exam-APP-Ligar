"""Auth router and JWT utilities.

Spec §4:
  POST /auth/student/login -> {access_token, student_id, name, class_name}
  POST /auth/teacher/login -> {access_token, teacher_id, name, role}

The JWT carries:
  sub  = string user id (UUID)
  role = 'student' | 'teacher' | 'admin' | 'owner' | 'homeroom'
  exp  = expiry unix timestamp (8 hours from issue)

This module also exports the dependencies you'll use everywhere else:
  - get_current_student  -> Student row
  - get_current_teacher  -> Teacher row
  - require_role(*roles) -> dependency factory; raises 403 on mismatch
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional

import bcrypt
import jwt  # PyJWT
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, utcnow
from models import Student, Teacher

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# JWT config
# ---------------------------------------------------------------------------

# Default is 32 bytes so PyJWT doesn't warn about HMAC key length in dev.
# Override with a random 32+ byte value (e.g. `openssl rand -base64 48`)
# for any non-local environment.
JWT_SECRET = os.environ.get(
    "JWT_SECRET",
    "dev-secret-change-me-in-prod-min-32-bytes-please",
)
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 8

# HTTPBearer ext: reads Authorization: Bearer <token>
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class StudentLoginResponse(BaseModel):
    access_token: str
    student_id: str
    name: str
    class_name: str


class TeacherLoginResponse(BaseModel):
    access_token: str
    teacher_id: str
    name: str
    role: str


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_jwt(sub: str, role: str, hours: int = JWT_EXPIRY_HOURS) -> str:
    """Issue a JWT. `sub` must be a string (UUID); JWT spec requires it."""
    now = utcnow()
    payload = {
        "sub": sub,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=hours)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    """Decode and validate a JWT. Raises 401 on any failure (expired,
    bad signature, malformed)."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token expired",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )


def _verify_password(plain: str, hashed: str) -> bool:
    """bcrypt verify. Returns False on any error (e.g. malformed hash)
    rather than raising — login should fail closed, not 500."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Login endpoints
# ---------------------------------------------------------------------------

@router.post("/student/login", response_model=StudentLoginResponse)
def student_login(body: LoginRequest, db: Session = Depends(get_db)):
    student = db.query(Student).filter_by(username=body.username).first()
    if not student or not _verify_password(body.password, student.password_hash):
        # Single error message regardless of which check failed -- don't
        # leak which usernames exist.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )
    if student.flagged:
        # Flagged students (esp. dup-NIS rows) shouldn't be able to log
        # in until kurikulum fixes the source. They're in the DB so admin
        # can see them, but their account is effectively locked.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account flagged for data review — contact wali kelas",
        )

    token = create_jwt(sub=str(student.id), role="student")
    return StudentLoginResponse(
        access_token=token,
        student_id=str(student.id),
        name=student.name,
        class_name=student.class_.name,
    )


@router.post("/teacher/login", response_model=TeacherLoginResponse)
def teacher_login(body: LoginRequest, db: Session = Depends(get_db)):
    teacher = db.query(Teacher).filter_by(username=body.username).first()
    if not teacher or not _verify_password(body.password, teacher.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )

    token = create_jwt(sub=str(teacher.id), role=teacher.role)
    return TeacherLoginResponse(
        access_token=token,
        teacher_id=str(teacher.id),
        name=teacher.full_name,
        role=teacher.role,
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies — use these in other routers
# ---------------------------------------------------------------------------

def _get_token_payload(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="expected Bearer scheme",
        )
    return decode_jwt(credentials.credentials)


def get_current_student(
    payload: dict = Depends(_get_token_payload),
    db: Session = Depends(get_db),
) -> Student:
    if payload.get("role") != "student":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="student role required",
        )
    student = db.query(Student).filter_by(id=payload["sub"]).first()
    if student is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="student no longer exists",
        )
    return student


def get_current_teacher(
    payload: dict = Depends(_get_token_payload),
    db: Session = Depends(get_db),
) -> Teacher:
    role = payload.get("role")
    if role not in {"teacher", "admin", "owner", "homeroom"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="teacher-class role required",
        )
    teacher = db.query(Teacher).filter_by(id=payload["sub"]).first()
    if teacher is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="teacher no longer exists",
        )
    return teacher


def require_role(*allowed: str):
    """Dependency factory. Use as: Depends(require_role('admin','owner'))."""
    allowed_set = set(allowed)

    def _check(payload: dict = Depends(_get_token_payload)) -> dict:
        if payload.get("role") not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role must be one of {sorted(allowed_set)}",
            )
        return payload

    return _check
