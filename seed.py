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
import difflib
import json
import re
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
    Base, Class, ClassSubject, Exam, Student, Subject, Teacher,
    TeacherSubject,
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
# §3.4  seed_teachers (from database/teachers.json)
# ---------------------------------------------------------------------------

# Subject names with this prefix (or that match these literal strings) aren't
# real subjects — Kepala Sekolah / Wakil Kepala / Bimbingan Konseling etc.
# We don't try to link them; they'd never have an Exam row anyway.
_NON_SUBJECT_NAMES = {
    "kepala sekolah",
    "wakil kepala sekolah",
    "wakil kepala kurikulum",
    "wakil kepala kesiswaan",
    "wakil kepala humas",
    "wakil kepala sarana prasarana",
    "kepala perpustakaan",
    "kepala laboratorium",
    "bimbingan konseling",
    "operator",
    "tata usaha",
}


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _norm_name(s: str) -> str:
    """Lowercase + collapse whitespace. Used for subject-name matching
    so 'Bahasa Indonesia ' and 'bahasa  indonesia' both match."""
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _fuzzy_subject_match(target: str, db_subjects: dict[str, Subject]) -> Subject | None:
    """Return the closest-matching Subject row for `target`, or None.

    Uses difflib at threshold 0.85 — high enough to catch typos like
    'Bahaasa Indonesia' vs 'Bahasa Indonesia' but not so loose that
    'Matematika Umum' matches 'Matematika Peminatan'.
    """
    nt = _norm_name(target)
    norm_keys = list(db_subjects.keys())
    best = difflib.get_close_matches(nt, norm_keys, n=1, cutoff=0.85)
    return db_subjects[best[0]] if best else None


def seed_teachers(json_path: str, db: Session) -> dict:
    """Idempotent. Creates/updates Teacher rows from a JSON file shaped:

        [{"kode": int, "nama": str, "nip": str (with spaces),
          "status": "PNS"|"PPPK"|...,
          "mata_pelajaran": [{"sub_kode": str, "mapel": str}, ...]}, ...]

    Username = NIP digits-only (uniquely school-issued, never collides).
    Initial password = last 6 digits of NIP — parallel to the student
    NISN-last-6 rule. Override SEED_BCRYPT_ROUNDS just like the student
    seeder does.

    Each (teacher, mapel) pair writes a TeacherSubject row, allowing
    multiple teachers to share a subject (the school has e.g. three
    Math teachers). The legacy Subject.teacher_id is also set for the
    first teacher to claim a subject, since admin reports use it as a
    'primary teacher' label.
    """
    stats = {
        "teachers_created": 0,
        "teachers_updated": 0,
        "subject_links_created": 0,
        "subject_links_existed": 0,
        "subjects_unmatched": 0,
        "non_subject_skipped": 0,
    }

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    # Build a dict keyed by normalized subject name so lookups handle
    # whitespace + case differences without difflib.
    db_subjects = {
        _norm_name(s.name): s
        for s in db.query(Subject).all()
    }

    for entry in data:
        nip_digits = _digits_only(entry.get("nip", ""))
        if not nip_digits:
            print(f"  skip: no NIP for {entry.get('nama')!r}", file=sys.stderr)
            continue

        existing = db.query(Teacher).filter_by(username=nip_digits).first()
        if existing is None:
            init_pw = nip_digits[-6:] if len(nip_digits) >= 6 else nip_digits
            t = Teacher(
                username=nip_digits,
                password_hash=_hash_password(init_pw),
                full_name=entry.get("nama", "").strip(),
                role="teacher",
            )
            db.add(t)
            db.flush()
            stats["teachers_created"] += 1
        else:
            t = existing
            new_name = entry.get("nama", "").strip()
            if new_name and t.full_name != new_name:
                t.full_name = new_name
                stats["teachers_updated"] += 1

        for m in entry.get("mata_pelajaran", []):
            mapel = (m.get("mapel") or "").strip()
            if not mapel:
                continue
            n_mapel = _norm_name(mapel)
            if n_mapel in _NON_SUBJECT_NAMES:
                stats["non_subject_skipped"] += 1
                continue

            subj = db_subjects.get(n_mapel) or _fuzzy_subject_match(mapel, db_subjects)
            if subj is None:
                stats["subjects_unmatched"] += 1
                print(
                    f"  unmatched subject: {mapel!r}  (teacher: {t.full_name})",
                    file=sys.stderr,
                )
                continue

            # Set legacy 1:N pointer for the first teacher to claim
            # this subject — kept around for admin "primary teacher"
            # views; authoring flows through TeacherSubject.
            if subj.teacher_id is None:
                subj.teacher_id = t.id

            existing_link = (
                db.query(TeacherSubject)
                .filter_by(teacher_id=t.id, subject_id=subj.id)
                .first()
            )
            if existing_link is None:
                db.add(TeacherSubject(teacher_id=t.id, subject_id=subj.id))
                stats["subject_links_created"] += 1
            else:
                stats["subject_links_existed"] += 1

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
    ap.add_argument("--teachers", default=None,
                    help="Optional path to teachers.json (creates Teacher rows "
                         "and wires Subject.teacher_id)")
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

        if args.teachers:
            print("\n== seed_teachers ==")
            s4 = seed_teachers(args.teachers, db)
            print(f"  {s4}")

        db.commit()
        print("\n== Committed. ==")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
