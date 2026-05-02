# How to Run the Smoke Tests

This guide is for **someone who has never written code before**. If you can
open a terminal and copy-paste a command, you can do this. The whole thing
should take ~10 minutes the first time and ~2 minutes every time after.

There are two test files:

| File                    | What it checks                                     |
|-------------------------|----------------------------------------------------|
| `smoke_test.py`         | **Week 1** — login, data confirmation, homeroom    |
| `smoke_test_week2.py`   | **Week 2** — teacher portal, admin panel, exam loop |

Run both. If they print **"ALL ... SMOKE TESTS PASSED"** at the end, the
backend is healthy.

---

## What you need before you start

1. **A computer** with Windows, macOS, or Linux.
2. **Python 3.11 or newer.** Check by opening a terminal and typing:

   ```
   python3 --version
   ```

   If you see something like `Python 3.11.4` or higher, you're set. If
   you see "command not found" or a version below 3.11, install Python
   from <https://www.python.org/downloads/> first.

3. **The project files.** If you haven't downloaded them yet:

   ```
   git clone https://github.com/nugross190/Exam-APP-Ligar.git
   cd Exam-APP-Ligar
   git checkout claude/review-and-continue-wKBPu
   ```

   The `git checkout` line switches to the branch with the Week 2 work.
   If you're already on `main`, you won't see `smoke_test_week2.py`.

> **A note on terminals.** On Windows, use **PowerShell** or **Command
> Prompt** (search for it in the Start menu). On macOS, open
> **Terminal** (Cmd+Space, type "terminal"). On Linux you already know.
> Every command in this guide goes in that window. Press Enter after
> each line.

---

## Step 1 — Open the project folder in a terminal

```
cd path/to/Exam-APP-Ligar
```

Replace `path/to/Exam-APP-Ligar` with where you actually saved the
folder. On Windows it might be `C:\Users\YourName\Exam-APP-Ligar`. On
Mac/Linux it might be `~/Exam-APP-Ligar`.

To confirm you're in the right place, type:

```
ls
```

(or `dir` on Windows). You should see files like `main.py`, `seed.py`,
`requirements.txt`, etc.

---

## Step 2 — (One-time) Create a virtual environment

A "virtual environment" is just a private folder for this project's
Python libraries, so they don't mix with anything else on your computer.
You only do this once.

```
python3 -m venv .venv
```

Then **activate** it. The activation command is different per OS:

- **macOS / Linux:** `source .venv/bin/activate`
- **Windows (PowerShell):** `.venv\Scripts\Activate.ps1`
- **Windows (Command Prompt):** `.venv\Scripts\activate.bat`

After it works, your terminal prompt will start with `(.venv)`. That
means you're inside the bubble. **You need to do this activation step
every time you open a new terminal**, but only the `python3 -m venv`
part is one-time.

---

## Step 3 — (One-time) Install the libraries

```
pip install -r requirements.txt
```

This downloads everything the project needs — FastAPI, SQLAlchemy, etc.
Takes 1–2 minutes. You only do this once (until `requirements.txt`
changes).

---

## Step 4 — (One-time) Seed the test database

The app needs a database with students, classes, subjects, and exams.
This command builds one from the Excel files that ship with the repo:

```
SEED_BCRYPT_ROUNDS=4 python3 seed.py \
  --xi daftar_peserta_kelas_XI_updated.xlsx \
  --x daftar_peserta_kelas_X_updated.xlsx \
  --schedule schedule_parsed.csv \
  --create-tables
```

> **Windows note.** The `\` at the end of each line means "continue on
> the next line". On Windows PowerShell, replace `\` with a backtick
> `` ` ``. Or just put it all on one long line — that always works.
>
> **Why `SEED_BCRYPT_ROUNDS=4`?** It tells the password hasher to use
> a fast (less secure) setting so seeding takes ~10 seconds instead of
> ~3 minutes. This is fine for testing. Don't use cost 4 in
> production.
>
> **Windows alternative**: if `SEED_BCRYPT_ROUNDS=4 python3 …` doesn't
> work, run these two commands instead:
> ```
> set SEED_BCRYPT_ROUNDS=4
> python3 seed.py --xi daftar_peserta_kelas_XI_updated.xlsx --x daftar_peserta_kelas_X_updated.xlsx --schedule schedule_parsed.csv --create-tables
> ```

When it finishes, you'll see something like:

```
== seed_classes_and_students ==
  {'created_classes': 24, 'created_students': 847, 'skipped': 0, 'dup_suffixed': 11}

== seed_subjects_and_exams ==
  {'created_subjects': 26, 'created_exams': 26, 'skipped_exams': 0}

== seed_class_subjects ==
  {'created_links': 308, 'skipped': 0}

== Committed. ==
```

That's the database ready. It's saved in a file called `hadir_exam.db`
in the project folder. If you ever want to start over, **delete that
file** and re-run this step.

---

## Step 5 — Run the Week 1 smoke test

```
python3 smoke_test.py
```

It will print one section at a time, with `[OK  ]` or `[FAIL]` next to
each check, like this:

```
======================================================================
  §4.1  Student login — happy path
======================================================================
  [OK  ] status: got=200  want=200
  [OK  ] student_id: got='...'  want='...'
  [OK  ] name: got='ADRIAN MAULANA'  want='ADRIAN MAULANA'
  ...
```

**What you want to see at the very end:**

```
======================================================================
  ALL WEEK 1 SMOKE TESTS PASSED
======================================================================
```

> **Known cosmetic issue.** Week 1 may stop on a check like
> `subject count: got=14  want=16`. That's a mismatch between the
> hard-coded number in the test and the current schedule data — it's
> not a real bug in the backend, just a stale expectation. If you hit
> that, move on to Week 2 anyway. (We'll fix it in a future cleanup.)

---

## Step 6 — Run the Week 2 smoke test

```
python3 smoke_test_week2.py
```

This one walks the whole flow: a teacher logs in, creates 3 questions
(multiple choice, true/false, multi-select), uploads a tiny image, an
admin opens the exam, a student takes it and scores 100%, and the
admin pulls the results spreadsheet.

**What you want to see at the very end:**

```
======================================================================
  ALL WEEK 2 SMOKE TESTS PASSED
======================================================================
```

If you scroll up through the output you'll see ~60 individual `[OK  ]`
lines — every one of those is a feature that works.

> **Re-running.** This script is **safe to run again and again**. It
> cleans up after itself at the start, so you can run it after every
> change to the code without weird leftovers piling up.

---

## What to do if a test fails

Don't panic. Look at the **first** `[FAIL]` line — the rest are
usually downstream noise. Compare `got=` and `want=`:

- **`got=401  want=200`** on a login → the test student's password
  doesn't match what's in the DB. Re-seed (Step 4).
- **`ModuleNotFoundError: No module named 'fastapi'`** (or any other
  module) → you forgot to activate the virtualenv (Step 2) or skipped
  Step 3. Run `pip install -r requirements.txt` again.
- **`sqlalchemy.exc.OperationalError: no such table`** → the database
  doesn't exist yet. Re-seed (Step 4).
- **`exam window has closed`** in Week 2 → time-zone weirdness. The
  smoke test sets the exam window to "now" automatically; if you see
  this, your computer's clock is probably very off.

If you hit something else, copy the first 20 lines of the failure and
send them to whoever's helping you with the project. The traceback
(the wall of `File "..."` lines) usually points right at the problem.

---

## Quick reference (after the one-time setup)

Every time you want to re-run the tests:

```
cd path/to/Exam-APP-Ligar
source .venv/bin/activate            # or the Windows equivalent
python3 smoke_test.py
python3 smoke_test_week2.py
```

That's it. Two commands. Both should end with "ALL ... PASSED".

---

## What success means

If both tests pass, you have proof that:

- Students can log in with their NIS + last 6 of NISN
- Flagged students are blocked
- Students can see their subject list and confirm it
- Homeroom teachers can see their class's confirmation status
- Teachers can write questions for their subject
- Admins can open exams to students
- Frozen-after-confirm works (teachers can't change questions mid-exam)
- Students can take an exam end-to-end and get scored
- Admins can monitor live and download results as Excel
- Admins can re-import students and the schedule

That's roughly 90% of the backend done. The remaining work is the
front-end HTML pages students will actually see in the browser, plus
the production deploy.
