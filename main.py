"""FastAPI app entrypoint.

Spec §10. Wires routers, CORS, and a health endpoint. As more routers
are built (exam, violation, teacher, admin) they'll be included here.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import SessionLocal, engine
from models import Base, Teacher
from routers import auth, confirm, exam, teacher, violation

app = FastAPI(
    title="HADIR Exam App",
    description="School exam administration system for SMAN 5 Garut",
    version="0.1.0",
)


@app.on_event("startup")
def _ensure_schema_and_bootstrap_admin() -> None:
    # No alembic migrations are configured yet; create any missing tables
    # so a fresh deploy doesn't 500 on the first DB query (e.g. login).
    Base.metadata.create_all(bind=engine)

    # Bootstrap an admin teacher when ADMIN_USERNAME / ADMIN_PASSWORD are
    # set, so the teacher portal is reachable on a clean DB. Idempotent:
    # only inserts if the username doesn't already exist.
    admin_user = os.environ.get("ADMIN_USERNAME")
    admin_pw = os.environ.get("ADMIN_PASSWORD")
    if not (admin_user and admin_pw):
        return
    import bcrypt
    db = SessionLocal()
    try:
        if db.query(Teacher).filter_by(username=admin_user).first() is not None:
            return
        db.add(Teacher(
            username=admin_user,
            password_hash=bcrypt.hashpw(
                admin_pw.encode("utf-8"), bcrypt.gensalt(rounds=10),
            ).decode("utf-8"),
            full_name=os.environ.get("ADMIN_FULL_NAME", admin_user),
            role=os.environ.get("ADMIN_ROLE", "admin"),
        ))
        db.commit()
    finally:
        db.close()

# CORS. During testing Railway will serve the static client from the same
# origin so this is mostly for local dev (file:// or 127.0.0.1).
# Tighten before production by reading allowed origins from env.
_cors_origins = os.environ.get("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers — Week 1 deliverables.
app.include_router(auth.router)
app.include_router(confirm.router)

# Week 2 - Core Exam Engine
app.include_router(exam.router)
app.include_router(violation.router)
app.include_router(teacher.router)

# Static mount for question images uploaded via /teacher/question/{id}/image.
# In production this should point at a Railway volume or be replaced by an
# R2/S3 redirect; the URL prefix is matched in routers/teacher.py.
_upload_dir = Path(os.environ.get("UPLOAD_DIR", "uploads")).resolve()
_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_upload_dir)), name="uploads")

# As you build more, uncomment as you go:
# app.include_router(admin.router)


@app.get("/")
def root():
    return {"app": "hadir-exam", "version": "0.1.0"}


@app.get("/health")
def health():
    return {"status": "ok"}
