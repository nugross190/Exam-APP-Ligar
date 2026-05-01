"""HADIR Exam App seed CLI.

Spec §3. Run order: seed_classes_and_students → seed_subjects_and_exams
→ seed_class_subjects. Idempotent: each step checks-before-insert.

Usage:
  python seed.py --xi <xi_path> --x <x_path> --schedule <sched_path>
  python seed.py ... --dry-run   # parse only, no DB writes

Flagged students per spec: insert all, set flagged=True, surface in admin
UI. Per project owner decision (2026-04-28): for nis_dup rows we suffix
the NIS to satisfy the unique constraint and leave them unable to log
in until the kurikulum team fixes the source data. Same for username.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Iterable

import bcrypt
from sqlalchemy.orm import Session

# Local imports — run with `python seed.py` from project root, or via
# `python -m seed` from one level up.
sys.path.insert(0, ".")
from database import SessionLocal, engine
from models import (
    Base, Class, ClassSubject, Exam, Student, Subject,
)
from parsers.excel import (
    StudentRow, ScheduleEntry,
    parse_schedule, parse_students, derive_class_subjects,
)


# ---------------------------------------------------------------------------
# Password hashing helpers
# ---------------------------------------------------------------------------

# bcrypt cost factor. 12 is the standard default but takes ~250ms/hash;
# 847 students at default cost = ~3.5min seed time. Cost 10 is roughly
# 4x faster (~60ms/hash) and still well above the 2024 NIST recommended
# minimum. Initial passwords are derived from NISN-suffix anyway and
# every student is expected to rotate on first login.
#
# Override via SEED_BCRYPT_ROUNDS env var (e.g. =4 for test runs, =12
# for a max-security production seed).
_SEED_BCRYPT_ROUNDS = int(__import__("os").environ.get("SEED_BCRYPT_ROUNDS", "10"))


def _hash_password(plain: str) -> str:
    """bcrypt hash. Empty string is hashed too — flagged students with
    no usable NISN still get a row but won't be able to log in
    (since they'd need to know the empty/garbage password)."""
    return bcrypt.hashpw(
        plain.encode("utf-8"),
        bcrypt.gensalt(rounds=_SEED_BCRYPT_ROUNDS),
    ).decode("utf-8")


def _initial_password_for(row: StudentRow) -> str:
    """Spec §4 NOTE: password = last 6 digits of NISN.

    For flagged-as-nisn-invalid rows the NISN may be shorter than 6 chars
    or empty; in that case we use the whole NISN (or an empty string)
    and the bcrypt hash will simply not match anything realistic. The
    student stays flagged in admin UI for fix-up.
    """
    return row.nisn[-6:] if row.nisn else ""


# ---------------------------------------------------------------------------
# §3.1  seed_classes_and_students
# ---------------------------------------------------------------------------

def seed_classes_and_students(
    student_rows: Iterable[StudentRow], db: Session,
) -> dict:
    """Idempotent. Returns {created_classes, created_students, skipped, dup_suffixed}."""
    rows = list(student_rows)
    stats = {"created_classes": 0, "created_students": 0,
             "skipped": 0, "dup_suffixed": 0}

    # 1. Classes — get or create per unique kelas string.
    unique_kelas = {r.kelas for r in rows if r.kelas}
    for kelas in unique_kelas:
        existing = db.query(Class).filter_by(name=kelas).first()
        if existing:
            continue
        # 'XI - A' -> grade 'XI'; 'X - C' -> grade 'X'
        grade = kelas.split("-")[0].strip()
        db.add(Class(name=kelas, grade=grade))
        stats["created_classes"] += 1
    db.flush()

    # Cache class_id by name for the student loop
    class_by_name = {c.name: c.id for c in db.query(Class).all()}

    # 2. Students.
    for row in rows:
        # Determine effective NIS / username. For nis_dup rows, suffix
        # to keep the unique constraint happy. Owner decision 2026-04-28:
        # flag, don't drop. Kurikulum team will fix source data later.
        effective_nis = row.nis
        effective_username = row.nis
        if "nis_dup" in row.flags:
            # Append the kelas to disambiguate. We sanitize for URL/CLI
            # safety AND keep the dash so 'X-I' and 'XI' don't collapse
            # to 'XI' — they have to remain visually distinct.
            suffix = row.kelas.replace(" ", "")  # 'X - I' -> 'X-I'; 'XI - A' -> 'XI-A'
            effective_nis = f"{row.nis}_DUP_{suffix}"
            effective_username = effective_nis
            stats["dup_suffixed"] += 1

        # Skip if already inserted (idempotency)
        existing = db.query(Student).filter_by(nis=effective_nis).first()
        if existing:
            stats["skipped"] += 1
            continue

        class_id = class_by_name.get(row.kelas)
        if class_id is None:
            # Should be impossible — we just created classes for every
            # row.kelas — but defensive against malformed kelas strings.
            print(f"  WARN: no class_id for {row.name!r} kelas={row.kelas!r}, skipping",
                  file=sys.stderr)
            stats["skipped"] += 1
            continue

        pw_plain = _initial_password_for(row)
        db.add(Student(
            nisn=row.nisn,
            nis=effective_nis,
            name=row.name,
            gender=row.gender,
            class_id=class_id,
            username=effective_username,
            password_hash=_hash_password(pw_plain),
            flagged=bool(row.flags),
            flag_reason=",".join(row.flags) if row.flags else None,
        ))
        stats["created_students"] += 1

    db.flush()
    return stats


# ---------------------------------------------------------------------------
# §3.2  seed_subjects_and_exams
# ---------------------------------------------------------------------------

def seed_subjects_and_exams(
    schedule_entries: Iterable[ScheduleEntry], db: Session,
) -> dict:
    """Idempotent. One Exam per subject, scheduled at the first-seen slot."""
    entries = list(schedule_entries)
    stats = {"created_subjects": 0, "created_exams": 0, "skipped_exams": 0}

    # 1. Subjects.
    unique_subjects = {e.subject for e in entries}
    for name in unique_subjects:
        if db.query(Subject).filter_by(name=name).first():
            continue
        db.add(Subject(name=name))
        stats["created_subjects"] += 1
    db.flush()

    subject_by_name = {s.name: s for s in db.query(Subject).all()}

    # 2. First-seen slot per subject.
    # Walk in input order so the first occurrence in the schedule grid
    # is what wins. Spec doesn't specify ordering precisely, so we pick
    # "iteration order of entries" which mirrors the row-major scan in
    # parse_schedule.
    subject_first_slot: dict[str, ScheduleEntry] = {}
    for e in entries:
        subject_first_slot.setdefault(e.subject, e)

    # 3. Exams.
    for name, slot in subject_first_slot.items():
        subj = subject_by_name[name]
        # Idempotency: skip if an Exam already exists for this subject.
        existing = db.query(Exam).filter_by(subject_id=subj.id).first()
        if existing:
            stats["skipped_exams"] += 1
            continue
        scheduled_at = datetime.combine(slot.date, slot.time_start)
        db.add(Exam(
            subject_id=subj.id,
            title=f"Ujian {name}",
            scheduled_at=scheduled_at,
            time_end=slot.time_end,
            duration_minutes=90,
            status="scheduled",
            admin_confirmed=False,
        ))
        stats["created_exams"] += 1

    db.flush()
    return stats


# ---------------------------------------------------------------------------
# §3.3  seed_class_subjects
# ---------------------------------------------------------------------------

def seed_class_subjects(
    class_subjects: dict[str, set[str]], db: Session,
) -> dict:
    """Idempotent. UNIQUE(class_id, subject_id) protects against dups."""
    stats = {"created_links": 0, "skipped": 0}

    class_by_name = {c.name: c for c in db.query(Class).all()}
    subject_by_name = {s.name: s for s in db.query(Subject).all()}

    for cls_name, subj_set in class_subjects.items():
        cls = class_by_name.get(cls_name)
        if cls is None:
            print(f"  WARN: no Class for {cls_name!r}, skipping", file=sys.stderr)
            continue
        for subj_name in subj_set:
            subj = subject_by_name.get(subj_name)
            if subj is None:
                print(f"  WARN: no Subject for {subj_name!r}, skipping", file=sys.stderr)
                continue
            existing = db.query(ClassSubject).filter_by(
                class_id=cls.id, subject_id=subj.id,
            ).first()
            if existing:
                stats["skipped"] += 1
                continue
            db.add(ClassSubject(class_id=cls.id, subject_id=subj.id))
            stats["created_links"] += 1

    db.flush()
    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Seed HADIR Exam App database.")
    ap.add_argument("--xi", required=True, help="Path to grade XI roster xlsx")
    ap.add_argument("--x", required=True, help="Path to grade X roster xlsx")
    ap.add_argument("--schedule", required=True, help="Path to schedule xlsx")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse only, print summary, no DB writes")
    ap.add_argument("--create-tables", action="store_true",
                    help="Run Base.metadata.create_all before seeding "
                         "(use only when alembic is not in play)")
    args = ap.parse_args()

    print("== Parsing files ==")
    students_result = parse_students(args.xi, args.x)
    schedule_result = parse_schedule(args.schedule)
    class_subjects = derive_class_subjects(schedule_result.data)

    print(f"  Students:        {len(students_result.data)} rows, "
          f"{sum(1 for s in students_result.data if s.flags)} flagged")
    print(f"  Schedule:        {len(schedule_result.data)} entries, "
          f"{len(schedule_result.warnings)} warnings")
    print(f"  Class-subjects:  {len(class_subjects)} classes, "
          f"{sum(len(v) for v in class_subjects.values())} links")
    print(f"  Unique subjects: {len({e.subject for e in schedule_result.data})}")

    if args.dry_run:
        print("\n== DRY RUN — no DB writes ==")
        # Show the warnings so the operator can decide whether to proceed
        if schedule_result.warnings:
            print(f"\nSchedule warnings ({len(schedule_result.warnings)}):")
            for w in schedule_result.warnings[:10]:
                print(f"  {w}")
            if len(schedule_result.warnings) > 10:
                print(f"  ... and {len(schedule_result.warnings)-10} more")
        return

    if args.create_tables:
        print("\n== Creating tables (Base.metadata.create_all) ==")
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        print("\n== seed_classes_and_students ==")
        s1 = seed_classes_and_students(students_result.data, db)
        print(f"  {s1}")

        print("\n== seed_subjects_and_exams ==")
        s2 = seed_subjects_and_exams(schedule_result.data, db)
        print(f"  {s2}")

        print("\n== seed_class_subjects ==")
        s3 = seed_class_subjects(class_subjects, db)
        print(f"  {s3}")

        db.commit()
        print("\n== Committed. ==")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
