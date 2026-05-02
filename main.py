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

from routers import admin, auth, confirm, exam, teacher, violation

app = FastAPI(
    title="HADIR Exam App",
    description="School exam administration system for SMAN 5 Garut",
    version="0.1.0",
)

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
app.include_router(admin.router)

# Static mount for question images uploaded via /teacher/question/{id}/image.
# In production this should point at a Railway volume or be replaced by an
# R2/S3 redirect; the URL prefix is matched in routers/teacher.py.
# Mount /uploads BEFORE the catch-all /static mount below so it wins.
_upload_dir = Path(os.environ.get("UPLOAD_DIR", "uploads")).resolve()
_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_upload_dir)), name="uploads")


@app.get("/health")
def health():
    return {"status": "ok"}


# Student client (spec §6.1 exam-client.html). Served at root with
# html=True so '/' returns static/index.html and SPA-style deep links
# fall back to the same page. Explicit FastAPI routes (auth, confirm,
# exam, etc.) are registered before this mount and so take precedence.
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="client")
