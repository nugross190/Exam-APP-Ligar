"""End-to-end smoke test for Week 1 endpoints.

Hits the FastAPI app via TestClient (no real network) and walks through
the user journey a student would take during the data confirmation
window:

  1. Login with NIS + last-6-of-NISN
  2. Get my-subjects, see the list
  3. Flag an error (e.g. 'I'm in the wrong class')
  4. Verify data_confirmed == False after flag
  5. Confirm
  6. Verify data_confirmed == True

Plus the homeroom path:
  7. Manually assign a homeroom teacher to a class
  8. Login as teacher
  9. Pull homeroom-summary, see the students with their confirmation status

Run with: python smoke_test.py
"""
import sys
sys.path.insert(0, "/home/claude")

import bcrypt
from fastapi.testclient import TestClient

from database import SessionLocal
from main import app
from models import Class, Student, Teacher

client = TestClient(app)


def banner(s):
    print(f"\n{'='*70}\n  {s}\n{'='*70}")


def assert_eq(label, got, want):
    ok = got == want
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}: got={got!r}  want={want!r}")
    if not ok:
        sys.exit(1)


def assert_true(label, cond):
    mark = "OK  " if cond else "FAIL"
    print(f"  [{mark}] {label}")
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Set up a known student we can log in as. Pick one from grade X (XI-A
# and X-A have no schedule). NIS=252610002 is ADELIA FEBRIANTI in X-A,
# but X-A has no exams. Use NIS=252610036 from X-B instead — let's
# look one up that's actually in a non-A class.
# ---------------------------------------------------------------------------

banner("Setup: pick a known good student")
db = SessionLocal()
try:
    # Find first non-flagged student in X-B
    s = (db.query(Student)
         .join(Student.class_)
         .filter(Class.name == "X - B", Student.flagged == False)
         .first())
    print(f"  Picked: {s.name}, kelas={s.class_.name}, nis={s.nis}, nisn={s.nisn}")
    test_username = s.username
    test_password = s.nisn[-6:]
    test_student_id = str(s.id)
    test_class_name = s.class_.name
    test_name = s.name
finally:
    db.close()


# ---------------------------------------------------------------------------
banner("§4.1  Student login — happy path")
r = client.post("/auth/student/login", json={
    "username": test_username, "password": test_password,
})
assert_eq("status", r.status_code, 200)
data = r.json()
assert_eq("student_id", data["student_id"], test_student_id)
assert_eq("name", data["name"], test_name)
assert_eq("class_name", data["class_name"], test_class_name)
assert_true("token present", "access_token" in data and len(data["access_token"]) > 20)

student_token = data["access_token"]
S_HEADERS = {"Authorization": f"Bearer {student_token}"}


# ---------------------------------------------------------------------------
banner("§4.1  Student login — wrong password")
r = client.post("/auth/student/login", json={
    "username": test_username, "password": "wrong",
})
assert_eq("status", r.status_code, 401)
assert_true("generic error message", "invalid credentials" in r.json()["detail"])


# ---------------------------------------------------------------------------
banner("§4.1  Student login — flagged user is locked out")
db = SessionLocal()
try:
    flagged = db.query(Student).filter_by(flagged=True).first()
    print(f"  Trying flagged student: {flagged.name} (kelas={flagged.class_.name})")
    flagged_user = flagged.username
    flagged_pw = (flagged.nisn or "")[-6:]
finally:
    db.close()
r = client.post("/auth/student/login", json={
    "username": flagged_user, "password": flagged_pw,
})
# Either 401 (bcrypt didn't verify because nisn was malformed) or
# 403 (bcrypt verified but flagged=True kicked in). Both are correct
# lockout behavior.
assert_true(f"flagged user blocked (got {r.status_code})",
            r.status_code in (401, 403))


# ---------------------------------------------------------------------------
banner("§9.1  GET /confirm/my-subjects — no token")
r = client.get("/confirm/my-subjects")
assert_eq("status", r.status_code, 401)


# ---------------------------------------------------------------------------
banner("§9.1  GET /confirm/my-subjects — valid token")
r = client.get("/confirm/my-subjects", headers=S_HEADERS)
assert_eq("status", r.status_code, 200)
data = r.json()
assert_eq("student_name", data["student_name"], test_name)
assert_eq("class_name",  data["class_name"], test_class_name)
assert_eq("data_confirmed (initial)", data["data_confirmed"], False)
print(f"  subjects: {len(data['subjects'])} (X-B should have 16)")
assert_eq("subject count", len(data["subjects"]), 16)
print("  first 3 subjects with exam slots:")
for sub in data["subjects"][:3]:
    print(f"    - {sub['name']:35s}  date={sub['exam_date']}  time={sub['time_start']}")


# ---------------------------------------------------------------------------
banner("§9.2  POST /confirm/flag-error — empty note rejected")
r = client.post("/confirm/flag-error", headers=S_HEADERS, json={"note": "  "})
assert_eq("status", r.status_code, 400)


# ---------------------------------------------------------------------------
banner("§9.2  POST /confirm/flag-error — happy path")
r = client.post("/confirm/flag-error", headers=S_HEADERS,
                json={"note": "Saya bukan kelas X-B, saya kelas X-C"})
assert_eq("status", r.status_code, 200)
assert_eq("data_confirmed after flag", r.json()["data_confirmed"], False)


# ---------------------------------------------------------------------------
banner("§9.3  POST /confirm/confirm — student confirms")
r = client.post("/confirm/confirm", headers=S_HEADERS)
assert_eq("status", r.status_code, 200)
assert_eq("data_confirmed", r.json()["data_confirmed"], True)

# verify in DB
db = SessionLocal()
try:
    s = db.query(Student).filter_by(id=test_student_id).first()
    assert_eq("DB row data_confirmed", s.data_confirmed, True)
finally:
    db.close()


# ---------------------------------------------------------------------------
banner("§9.4  Homeroom summary — setup teacher, assign to X-B")
db = SessionLocal()
try:
    # Create a teacher with role='homeroom' if not present
    t = db.query(Teacher).filter_by(username="walikelas_xb").first()
    if t is None:
        t = Teacher(
            username="walikelas_xb",
            password_hash=bcrypt.hashpw(
                b"testpass123", bcrypt.gensalt(rounds=4)
            ).decode("utf-8"),
            full_name="Bu Wali X-B",
            role="homeroom",
        )
        db.add(t)
        db.flush()

    # Assign as homeroom of X-B
    cls_xb = db.query(Class).filter_by(name="X - B").first()
    cls_xb.homeroom_teacher_id = t.id
    db.commit()

    teacher_username = t.username
finally:
    db.close()

# Login as teacher
r = client.post("/auth/teacher/login", json={
    "username": teacher_username, "password": "testpass123",
})
assert_eq("teacher login status", r.status_code, 200)
teacher_token = r.json()["access_token"]
T_HEADERS = {"Authorization": f"Bearer {teacher_token}"}

# Hit homeroom-summary
r = client.get("/confirm/homeroom-summary", headers=T_HEADERS)
assert_eq("homeroom-summary status", r.status_code, 200)
data = r.json()
assert_eq("class_name in summary", data["class_name"], "X - B")
print(f"  X-B has {len(data['students'])} students in summary")
assert_true("student count > 30", len(data["students"]) > 30)

# Confirm our test student appears in the summary with data_confirmed=True
test_row = next(
    (r for r in data["students"] if r["student_id"] == test_student_id),
    None,
)
assert_true("test student appears in homeroom summary", test_row is not None)
assert_eq("test student data_confirmed in summary",
          test_row["data_confirmed"], True)
print(f"  Test student row: {test_row}")


# ---------------------------------------------------------------------------
banner("§9.4  Homeroom summary — student token rejected")
r = client.get("/confirm/homeroom-summary", headers=S_HEADERS)
# Student role is not in {teacher, admin, owner, homeroom}
assert_eq("student gets 403", r.status_code, 403)


# ---------------------------------------------------------------------------
banner("ALL WEEK 1 SMOKE TESTS PASSED")
