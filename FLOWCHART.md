# HADIR Exam App — Flowcharts

Mermaid diagrams of the main flows in this repo. GitHub renders them
inline. Update them whenever a router gains/loses an endpoint or a
state machine changes — they're documentation, not code, and they rot
fast if you don't.

---

## 1. System overview

```mermaid
flowchart LR
    subgraph Clients
        S[Student browser]
        T[Teacher browser]
        A[Admin/Owner browser]
        H[Homeroom browser]
    end

    subgraph FastAPI["FastAPI app  (main.py)"]
        AUTH["routers/auth.py<br/>§4 login + JWT"]
        CONF["routers/confirm.py<br/>§9 data confirmation"]
        EX["routers/exam.py<br/>§5 exam engine"]
        VIO["routers/violation.py<br/>§6 violations + panic"]
        TCH["routers/teacher.py<br/>§7 question CRUD"]
        ADM["routers/admin.py<br/>§8 admin panel"]
        UP[/uploads<br/>StaticFiles mount/]
    end

    DB[(SQLite / Postgres<br/>via SQLAlchemy)]
    VOL[/UPLOAD_DIR<br/>local volume or R2/]

    S --> AUTH & CONF & EX & VIO
    T --> AUTH & TCH
    A --> AUTH & ADM
    H --> AUTH & CONF & VIO

    AUTH & CONF & EX & VIO & TCH & ADM --> DB
    TCH -- writes images --> VOL
    UP -- serves --> VOL
    S -. fetches image .-> UP
```

---

## 2. Endpoint map by spec section

```mermaid
flowchart TB
    subgraph Week1["Week 1 — foundation"]
        direction LR
        A1["§4.1 POST /auth/student/login"]
        A2["§4.2 POST /auth/teacher/login"]
        C1["§9.1 GET /confirm/my-subjects"]
        C2["§9.2 POST /confirm/flag-error"]
        C3["§9.3 POST /confirm/confirm"]
        C4["§9.4 GET /confirm/homeroom-summary"]
    end

    subgraph Week2["Week 2 — exam engine + portals"]
        direction LR
        E1["§5.1 POST /exam/start"]
        E2["§5.2 GET  /exam/{id}"]
        E3["§5.3 GET  /exam/{id}/questions"]
        E4["§5.4 POST /exam/{id}/answer"]
        E5["§5.5 POST /exam/{id}/submit"]
        V1["§6.2 POST /violation/{id}"]
        V2["§6.3 POST /violation/{id}/panic"]
        V3["§6.4 GET  /violation/{id}"]
        T1["§7.1 GET  /teacher/exams"]
        T2["§7.2 POST /teacher/exam/{id}/question"]
        T3["§7.3 PUT  /teacher/question/{id}"]
        T4["§7.4 POST /teacher/question/{id}/image"]
        D1["§8.1 POST /admin/exam/{id}/confirm"]
        D2["§8.2 GET  /admin/exam/{id}/monitor"]
        D3["§8.3 GET  /admin/results/{id}/export"]
        D4["§8.4 POST /admin/import/students"]
        D5["§8.5 POST /admin/import/schedule"]
    end
```

---

## 3. Student exam-day flow (§4 → §5 → §6)

The happy path plus the two terminal branches (panic, expelled).

```mermaid
flowchart TD
    L[POST /auth/student/login] --> L_OK{flagged?}
    L_OK -- yes --> L_BLK[403 — locked out]
    L_OK -- no --> JWT[JWT issued, 8h]

    JWT --> ST[POST /exam/start]
    ST --> ST_C{exam.admin_confirmed<br/>and in window?}
    ST_C -- no --> ST_X[400 — not yet open / closed]
    ST_C -- yes --> SESS[(ExamSession created<br/>status='active'<br/>question_order shuffled)]

    SESS --> Q[GET /exam/{id}/questions]
    Q --> ANS[POST /exam/{id}/answer<br/>repeat per question]
    ANS --> ANS

    ANS --> CHOICE{Student action}
    CHOICE -- normal --> SUB[POST /exam/{id}/submit]
    CHOICE -- tab switch /<br/>fullscreen exit --> VIO[POST /violation/{id}]
    CHOICE -- emergency --> PAN[POST /violation/{id}/panic]

    VIO --> VIO_T{violation_count}
    VIO_T -- 1 --> ANS
    VIO_T -- 2 --> LOCK[locked_until = now+30s]
    LOCK --> ANS
    VIO_T -- ≥3 --> EXP[ExpelledFlag created<br/>status='expelled']

    SUB --> SCORE[score per §1.4<br/>ExamResult written]
    SCORE --> DONE[200 — total/max/percentage]
    PAN --> END_PAN[status='panic']
    EXP --> END_EXP[homeroom notified<br/>via /admin/monitor]
```

---

## 4. Teacher authoring → admin confirm → frozen

This is the handoff that makes the question set tamper-proof during
the exam window. Once the admin confirms, every mutating teacher
endpoint returns 400.

```mermaid
sequenceDiagram
    autonumber
    participant T as Teacher
    participant API as FastAPI
    participant DB as DB
    participant A as Admin

    T->>API: POST /auth/teacher/login
    API-->>T: JWT (role='teacher')

    T->>API: GET /teacher/exams
    API->>DB: SELECT exams WHERE subject.teacher_id = me
    DB-->>API: rows
    API-->>T: list

    loop write each question
        T->>API: POST /teacher/exam/{id}/question
        API->>API: validate (type, choices_count,<br/>≥1 correct, etc.)
        API->>DB: INSERT Question + Choice rows<br/>weight = 1/correct_n
        DB-->>API: ok
        API-->>T: 201 {question_id}
    end

    T->>API: POST /teacher/question/{id}/image
    API->>API: write file to UPLOAD_DIR
    API-->>T: {image_url: /uploads/...}

    A->>API: POST /admin/exam/{id}/confirm
    API->>DB: exam.admin_confirmed = true<br/>exam.status = open|scheduled
    API-->>A: 200

    rect rgba(220, 0, 0, 0.08)
    note over T,DB: Frozen-after-confirm
    T->>API: PUT /teacher/question/{id}
    API->>DB: read exam.admin_confirmed
    DB-->>API: true
    API-->>T: 400 — questions are frozen
    end
```

---

## 5. ExamSession state machine

```mermaid
stateDiagram-v2
    [*] --> pending: row created on /exam/start<br/>(brief, normally skipped)
    pending --> active: started_at set,<br/>question_order generated
    active --> submitted: POST /exam/.../submit
    active --> expelled: violation_count ≥ 3
    active --> panic: POST /violation/.../panic
    submitted --> [*]
    expelled --> [*]
    panic --> [*]

    note right of active
        locked_until may be set on
        2nd violation (30s timeout).
        Lock is checked by GET questions
        and POST answer endpoints.
    end note
```

---

## 6. Violation threshold (§6.2)

```mermaid
flowchart LR
    EVT[tab_switch /<br/>fullscreen_exit event] --> POST[POST /violation/{id}]
    POST --> INC[violation_count += 1<br/>SessionViolation row]
    INC --> SW{count?}
    SW -- 1 --> W[200 — warning]
    SW -- 2 --> LCK[locked_until = now+30s<br/>200 — locked]
    SW -- ≥3 --> EXP[status = expelled<br/>ExpelledFlag created<br/>200 — expelled]

    LCK -. blocks .-> Q[GET questions]
    LCK -. blocks .-> ANS[POST answer]
    EXP -. surfaces in .-> MON[GET /admin/monitor]
    EXP -. visible to .-> HR[homeroom teacher<br/>via class_id link]
```

---

## 7. Data confirmation flow (§9)

The pre-exam window where students verify their subject list and the
homeroom teacher reviews the class summary.

```mermaid
flowchart TD
    SL[POST /auth/student/login] --> MY[GET /confirm/my-subjects]
    MY --> CK{list correct?}
    CK -- yes --> CONF[POST /confirm/confirm<br/>data_confirmed = true]
    CK -- no --> FLAG[POST /confirm/flag-error<br/>DataFlag created<br/>data_confirmed = false]
    FLAG -. fix at source .-> SL

    HL[POST /auth/teacher/login<br/>role='homeroom'] --> SUM[GET /confirm/homeroom-summary]
    SUM --> ROWS[per-student rows<br/>name, NIS, subject_count,<br/>data_confirmed, flagged]
```

---

## 8. Imports / re-imports (§8.4–§8.5)

```mermaid
flowchart LR
    subgraph Source files
        XI[daftar_peserta_kelas_XI.xlsx]
        X[daftar_peserta_kelas_X.xlsx]
        SC[schedule_parsed.csv]
    end

    XI & X --> IS[POST /admin/import/students<br/>?dry_run=true|false]
    SC --> ISC[POST /admin/import/schedule<br/>?dry_run=true|false]

    IS --> PS["parsers.excel.parse_students()<br/>flags dups, NISN length"]
    ISC --> PSC["parsers.excel.parse_schedule()<br/>+ derive_class_subjects()"]

    PS --> DRY1{dry_run?}
    DRY1 -- yes --> R1[return counts + warnings]
    DRY1 -- no --> S1["seed.seed_classes_and_students()"]
    S1 --> DB[(DB)]

    PSC --> DRY2{dry_run?}
    DRY2 -- yes --> R2[return counts + warnings]
    DRY2 -- no --> S2["seed.seed_subjects_and_exams()<br/>+ seed_class_subjects()"]
    S2 --> DB
```
