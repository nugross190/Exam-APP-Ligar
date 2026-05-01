# Week 1 Review Checklist — HADIR Exam App

You're reviewing 6 files (~1,400 lines total). The point of this review
is **not** to check whether the code works — the smoke test already
passed. The point is **you understand what's there**, so you can debug
it when something breaks at 09:30 on December 8th.

---

## How to use this document

For each file, work through the questions in order. Don't move on until
you can answer in your own words. If you can't, write the question down
and ask Claude in the next session.

**Estimated total time: 4–5 hours, spread across the rest of this week.**

Suggested split:
- **Wednesday:** files 1–3 (parser, models, database) — 2.5h
- **Thursday:** files 4–5 (seed, auth) — 2h
- **Friday:** file 6 (confirm) + run everything yourself — 1h

---

## File 1: `parsers/excel.py` (230 lines)

This is the foundation — every count downstream depends on this parser
being correct.

**Questions you should be able to answer:**

1. The function `parse_students` returns a `ParseResult`. What is in
   `ParseResult.data`? What is in `ParseResult.warnings`? Why is this
   shape useful instead of just returning a list?

2. In `parse_students`, why are `nisn_seen` and `nis_seen` declared
   **outside** the for-loop over files? What would change if they were
   declared inside?

3. In `_build_slots`, why does the loop start at `c = 1` instead of
   `c = 0`?

4. The XI block in the schedule has 14 schedule columns; the X block
   has 16. How does the parser handle this difference without you
   hardcoding either number anywhere?

5. What is the special-case in `parse_id_date` that handles
   `"Jum'at, 12 Desember 2025"` correctly? (Hint: look at the `split`
   and the `strip`.)

**Quick exercise:** Look at the placeholder check in `parse_schedule`.
What would happen if the source file had `'Column17'` in some cell?
Would the parser correctly flag it as a placeholder? (Answer: no — it
only knows up to Column16. This is a bug-in-waiting if the schedule
ever has more than 16 columns.)

---

## File 2: `models.py` (230 lines, 15 classes)

You don't need to read every class in detail. Focus on these:

**Read carefully:** `Class`, `Teacher`, `Student`, `Subject`,
`ClassSubject`, `Exam`, `ExamSession`.

**Skim only:** `Question`, `Choice`, `StudentAnswer`, `AnswerChoice`,
`SessionViolation`, `ExamResult`, `DataFlag`, `ExpelledFlag`.

**Questions:**

1. The `Student` model has `class_id` (FK to Class). It also has
   `class_` (a `relationship`). What's the difference? When would you
   use one vs the other in code?

2. `ClassSubject` has a `UniqueConstraint` on `(class_id, subject_id)`.
   What does this prevent? What error would you see if you tried to
   insert a duplicate?

3. Why is `homeroom_teacher_id` on `Class` (not `teacher_id` on
   `Student`)? Hint: a class has one homeroom teacher; a teacher might
   teach many classes but is homeroom of at most one.

4. The spec said 13 models. I made 15. Which two did I add and why?
   (Answer: `DataFlag`, `ExpelledFlag`. Both are referenced in spec
   §9 and §6 but not defined in §1.)

5. Why is `username` unique on Student even though `nis` is also unique?
   Couldn't you just use `nis` as the username and skip a column?
   (Answer: yes you could — but the dup-NIS suffixed students get a
   different username from their original NIS, and the model needs to
   accommodate that without breaking the unique-username rule.)

---

## File 3: `database.py` (35 lines)

Trivial. Just read it once.

**Questions:**

1. What does `get_db()` do? Why does it use `yield` instead of `return`?
   (Answer: yield lets FastAPI close the session after the request,
   even if an exception was raised.)

2. The file has a special case for `postgres://` URIs. What does it do
   and why?

---

## File 4: `seed.py` (280 lines)

This is the longest and most decision-laden file. Read it slowly.

**Questions:**

1. The function `seed_classes_and_students` does **two** loops over
   different things. What does each loop do, and why must they be in
   that order?

2. Look at the dup-NIS handling. Find one of the 11 dup-suffixed
   students in the smoke test output. Their NIS ends with `_DUP_X-L`.
   Can they log in? Why or why not? (Answer: no — they're flagged, and
   the login endpoint blocks flagged students with 403.)

3. The `seed_subjects_and_exams` function uses
   `subject_first_slot.setdefault(e.subject, e)`. What does `setdefault`
   do? Why is this the right operation here? (Hint: think about what
   would happen with `subject_first_slot[e.subject] = e` instead.)

4. The CLI has a `--dry-run` flag. What does it do, and why is it
   important when running this against a production DB? (Answer: lets
   you see warnings and counts before mutating any rows.)

5. The bcrypt cost factor is read from an environment variable. What
   value would you use for a Railway production deploy? What would you
   use locally for fast iteration? (Answer: 10–12 for prod; 4 for
   local tests.)

---

## File 5: `routers/auth.py` (220 lines)

This is the security perimeter — every other router depends on it.

**Questions:**

1. The `_get_token_payload` dependency is private (underscore prefix).
   What public dependencies use it? Why is the private/public split
   useful? (Answer: it isolates "did the request have a valid JWT" from
   "what is this user allowed to do" — single responsibility.)

2. What's the difference between `get_current_student` and
   `get_current_teacher`? When would you use `require_role(...)` instead
   of either?

3. In `student_login`, the error message is
   `"invalid credentials"` for both "username doesn't exist" and
   "password is wrong." Why is this important? (Answer: stops attackers
   from enumerating valid usernames.)

4. Look at `_verify_password`. It catches `ValueError` and `TypeError`
   and returns `False`. What kind of input would trigger that?
   (Answer: a malformed bcrypt hash — e.g. an empty string. The login
   endpoint should fail-closed, not 500.)

5. The JWT contains `sub` (subject), `role`, `iat` (issued at), `exp`
   (expiry). What happens if a student's session is hijacked? How long
   is the attacker's window? (Answer: 8 hours. This is the spec's
   choice; you could shorten it if exam-day requires.)

---

## File 6: `routers/confirm.py` (180 lines)

The Week 1 done-when condition lives here: "Student sees correct subject
list."

**Questions:**

1. The `my_subjects` query uses `outerjoin` for the Exam table, not
   `join`. What's the difference, and what would happen if I'd used
   `join` here instead? (Answer: an XI-A student would see zero subjects
   instead of seeing their subjects with null exam dates — they'd never
   know there's a problem.)

2. The `flag_error` endpoint sets `data_confirmed = False`. Why?
   What scenario does this protect against? (Answer: a student
   confirms, then later spots a mistake; their old confirmation
   shouldn't count.)

3. In `homeroom_summary`, the `subject_count` is computed once,
   outside the loop over students. Why? (Answer: every student in a
   class has the same subjects, so it's a single query, not 36.)

4. What happens if a teacher with role='admin' tries to hit
   `/confirm/homeroom-summary`? Does it work? Should it?

---

## End-to-end self-check

Once you've read everything, run this yourself on your laptop:

```bash
# 1. Install
pip install -r requirements.txt

# 2. Seed (use cost 4 for fast iteration)
SEED_BCRYPT_ROUNDS=4 python seed.py \
  --xi /path/to/daftar_peserta_kelas_XI_updated.xlsx \
  --x /path/to/daftar_peserta_kelas_X_updated.xlsx \
  --schedule /path/to/schedule_sample.xlsx \
  --create-tables

# 3. Run smoke test
python smoke_test.py

# 4. Start the app and poke at it manually
uvicorn main:app --reload
# Then in another terminal:
curl http://localhost:8000/health
curl -X POST http://localhost:8000/auth/student/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<a real NIS>","password":"<last 6 of NISN>"}'
```

If smoke test passes and you can hit the endpoints with curl, Week 1
is done.

---

## What's NOT in Week 1 (Week 2 onward)

This is intentionally minimal. The Week 2 done-when conditions need:

- `routers/teacher.py` — question CRUD + image upload
- `routers/exam.py` — start, serve questions, save answer, submit, score
- `routers/violation.py` — tab-switch lockdown, panic button
- `routers/admin.py` — confirm exam, monitor live, import CSV

None of those exist yet. Week 1 ships the foundation; Week 2 builds the
exam engine on top.

---

## Red flags to ask Claude about

If any of these are true, **don't move to Week 2 until they're resolved**:

- [ ] You can't explain why `Class.homeroom_teacher_id` is on Class,
      not on Teacher.
- [ ] You can't explain what `outerjoin` does differently from `join`.
- [ ] You don't know what would happen if you tried to log in with a
      flagged student's NIS.
- [ ] The smoke test fails on your laptop (it should pass cleanly).
- [ ] You don't know which file you'd edit to change the JWT expiry
      from 8 hours to 4 hours.
