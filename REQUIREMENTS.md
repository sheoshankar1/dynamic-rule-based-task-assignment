# Requirements Traceability

Every line of the brief, mapped to where it is implemented, how it was verified, and why
that route was chosen over the alternative.

**Legend:** ✅ done and verified · ⚠️ done with a declared limitation · ❌ not done

Architecture rationale lives in [README.md](README.md) (decisions D1–D17); delivery history in
[ROADMAP.md](ROADMAP.md); the evidence ledger separating *measured* from *reasoned* is
README §12.

---

## 1. Suggested tech stack

| Asked for | Used | Why |
|---|---|---|
| Python (Django or FastAPI) | ✅ Django 4.2 + DRF | The work is model-heavy: migrations, a custom user, an admin, an ORM for the 80% of queries that are ordinary. FastAPI would have meant assembling those separately for no gain on the one part that is genuinely hard, which is SQL that neither framework writes for you |
| PostgreSQL | ✅ 14 | Partial indexes, `RETURNING`, and mixed ASC/DESC composite indexes are all load-bearing here (§4). The design would not port to MySQL unchanged |
| Redis — caching & queues | ✅ both | Celery broker, rule-spec cache, single-flight lock, recompute debounce |
| Celery / RQ / Worker | ✅ Celery + beat | Beat runs in the worker process (`-B`): the two periodic jobs are low-frequency safety nets, not throughput work, so a separate container would be ceremony |
| JWT + refresh tokens | ✅ SimpleJWT | `/auth/login`, `/auth/refresh`; the UI refreshes once transparently on a 401, then gives up rather than looping |
| React | ✅ React 18 + Vite | Deliberately thin — see §7 |
| Docker & Docker Compose | ✅ verified running | Five services; `docker compose up --build` exercised, not just written (§8) |

**Where Redis is deliberately *not* used:** caching the per-rule eligible set. It was in the
design until measurement killed it — the query it would replace runs at p95 1.65 ms, and the
volatile capacity filter has to hit the database regardless, so the cache would add an
invalidation surface to save nothing. README §7 keeps the retraction visible rather than
quietly dropping it.

---

## 2. Core features

### 2.1 Authentication & authorization

| Requirement | Status | Where | Why this route |
|---|---|---|---|
| User signup | ✅ | `POST /auth/signup` | Creating a user fires the stable-attribute signal, so a new account is evaluated against every rule and can immediately pick up pooled work. That is the "user created" trigger from §6, not a special case bolted onto signup |
| Login | ✅ | `POST /auth/login`, `/auth/refresh` | |
| Roles: Admin, Manager, User | ✅ | `User.Role`, enforced per endpoint | The brief names three roles and defines two. Resolved with you: **Managers author rules and tasks; Admin is system administration** (user management, the recompute escape hatch); Users receive work. Recorded in README §11 as resolved-by-user, not inferred |

Authorization is enforced at three points rather than one: authoring tasks and rules
(Manager/Admin), the recompute escape hatch (Admin only), and list/detail visibility (a User
sees only their own work). ⚠️ **JWTs are stored in `localStorage`** — adequate for an internal
admin surface, vulnerable to XSS; httpOnly cookies with CSRF protection would be the
production choice. Declared in README §13 rather than left as an oversight.

### 2.2 Task management

| Requirement | Status | Where |
|---|---|---|
| Create | ✅ | `POST /tasks/` |
| Read | ✅ | `GET /tasks/` (filtered, paginated, role-scoped), `GET /tasks/{id}` |
| Update | ✅ | `PATCH /tasks/{id}` — task fields, status, or rules |
| Delete | ✅ | `DELETE /tasks/{id}` |
| Status: Todo → In Progress → Done | ✅ | `PATCH` for todo↔in_progress; `POST /tasks/{id}/complete` for terminal |
| Due dates and priority | ✅ | `due_date`, `priority smallint` |

**Why terminal states are not reachable through `PATCH`.** Setting `status = done` moves
effort from `committed_effort_hours` to `lifetime_hours` and frees a capacity slot. If a
generic field edit could set that status, the counters would silently drift — the assignee
would look permanently busier than they are, skewing every later selection, with nothing to
surface it. So `OPEN_TRANSITIONS` allows only `todo ↔ in_progress`, and reaching a terminal
state goes through `complete_task()` where the bookkeeping happens in one transaction. The
error message says exactly that rather than just refusing.

**Why deleting an assigned task releases capacity first.** Same failure mode: a deleted task
whose counters were never decremented inflates its assignee forever. `delete_task()` releases
then deletes, in one transaction, and a test asserts the counters return to zero.

**Why `priority` is a `smallint`, not a text enum.** Drain order is `ORDER BY priority ASC`.
Text sorts lexically, so `'P10'` would sort before `'P2'` — silently wrong the moment
priorities pass single digits.

### 2.3 Dynamic rule-based assignment engine

| Requirement | Status | Where |
|---|---|---|
| Tasks are NOT manually assigned | ✅ | No endpoint accepts an assignee. `fill_capacity()` is the only code path that sets one |
| Each task defines dynamic assignment rules | ✅ | `rules` on `POST /tasks/`, repointed by `PATCH` |

**Why there is no rule DSL.** The attribute set is closed and known (four fields). A general
expression language means a grammar, a parser, an AST, an evaluator and a safety story, bought
to express predicates that fit in a struct. It would also break the deduplication the whole
design rests on: arbitrary expressions have unbounded cardinality, so identical-in-meaning
rules would stop sharing a fingerprint. The compiler is ~40 lines. Stated ceiling in README
§2: if boolean nesting is ever genuinely required, the migration is a rule tree with the *same*
stable/volatile split — the split is the load-bearing idea, not the flat shape.

**Why predicates are single-valued.** One department, at most one location, no OR anywhere.
More than one value is **rejected, not truncated** — a rule that quietly dropped a department
would route work to the wrong team with nothing to show for it. A one-element list is accepted
and flattened for older clients, and a repeated value (`["HR","HR"]`) dedupes rather than
erroring. This also shrinks the reachable rule space from a powerset to a product, which makes
rules repeat more and deduplication work harder.

---

## 3. User profile attributes

| Attribute | Field | Role in the design |
|---|---|---|
| Department | `department` | **Stable** — fingerprinted, materialised |
| Experience in years | `experience_years` | **Stable** |
| Location | `location` | **Stable** |
| Current number of assigned tasks | `active_task_count` | **Volatile** — the capacity cap, never materialised |

**This split is the central architectural decision (D2).** `active_tasks < 5` changes on every
single assignment. If it participated in materialised eligibility, one assignment would
invalidate every rule set the assignee belongs to and the system would spend its life
recomputing itself. Stable predicates are materialised into `rule_eligible_user`; volatile
fields are applied as a `WHERE` clause at query time.

**Evidence the boundary sits in the right place:** two selection dimensions
(`committed_effort_hours`, `lifetime_hours`) were added late in the build and required *no
change* to the materialised table, because they are volatile and never entered the fingerprint.

---

## 4. User stories

### Story 1 — Admin creates a task with rules; the system assigns in the background

✅ `POST /tasks/` → task enters the pool → `fill_capacity` places it in a Celery worker.

**"What happens if there are multiple eligible users?"** A four-key deterministic ladder:

```sql
ORDER BY committed_effort_hours ASC,   -- 1. least current load, in HOURS
         lifetime_hours         DESC,  -- 2. more delivered wins the tie
         date_joined            ASC,   -- 3. older account
         id                     ASC    -- 4. total order
```

Key 1 is measured in effort hours, not task count, because "least loaded" is meaningless if
one 8-hour task counts the same as four 30-minute ones. Keys 1 and 2 oppose deliberately:
least *current* burden, most *lifetime* delivered.

Key 4 is an addition, declared: `date_joined` is not unique — a bulk import stamps many rows
identically — so without a unique final key the order is not total and identical state returns
whatever the engine feels like. **Measured: 17 users tied the winner on keys 1–3.** Key 4 is
not decoration.

Round-robin was **rejected on evidence, not preference**: measured at 24.7 ms against the
ladder's 31.9 ms on the same plan — 7 ms of a 32 ms query, almost all row width. It buys ~20%
on a query whose cost lay entirely in the join plan, and forfeits the least-load property to
get it.

**"What happens if there are no eligible users?"** The task is created and pooled — never
rejected. A rule matching nobody *today* is a timing problem, not a validation error: the new
hire arrives Monday, the current assignee finishes at 4pm. Two causes are reported distinctly,
because they need opposite human responses:

```
eligible_count = 0  ->  STRUCTURAL: no user matches this rule. Fix the rule.
eligible_count > 0  ->  TRANSIENT: all matching users at capacity. Wait.
```

A third state exists and is also reported honestly: **`pending`** — placement is queued and
has not run. An earlier version guessed here and returned "43 users match, all at capacity"
for a task the worker assigned one second later. `placement_attempted_at` now makes the
distinction a fact on the row rather than an inference.

### Story 2 — User views eligible tasks, highly optimised

✅ `GET /my-eligible-tasks`.

**Interpretation, resolved with you:** "eligible **and** assigned" is the conjunction. A user
sees a task only once it is assigned to them; there is no self-service pool. Assignment
already implies eligibility, so the query is a bounded index lookup on
`tasks (assignee, priority, due_date) WHERE status <> terminal`.

**This inverted an earlier claim of mine.** I had modelled this endpoint as the one with real
fan-out and built a Redis payload cache around it. Under the conjunction reading it returns at
most a user's task cap — single digits. **Measured p95: 0.38 ms.** The endpoint the brief
singles out as needing heavy optimisation needs almost none; the real cost is in the
*assignment* path, which the brief never mentions.

### Story 3 — User data changes, eligibility recomputes automatically

✅ `post_save` signal → debounced `recompute_user`.

Only **stable** attributes trigger anything; the volatile columns change on every assignment
and trigger nothing, by construction. Change detection uses `Model.from_db` to snapshot at
load time, so no second query is needed, and a save naming only volatile fields exits
immediately — that is the common path by a wide margin.

`recompute_user` evaluates one user against every cached rule and writes **only the delta**.
Not an inverted index over predicates: testing one user against ~10³ cached rules is a
microsecond loop, and an index would be more code, more invalidation surface, and slower at
this cardinality. The delta matters because gaining a rule is an explicit event — it is what
re-queues pooled work.

⚠️ **Declared limitation:** `queryset.update()` and `bulk_create()` do not send signals. That
is Django's contract, documented in `signals.py`; the seed commands call `recompute_user`
themselves.

### Story 4 — Admin updates rules, recompute efficiently

✅ `PATCH /tasks/{id}` with `rules`.

**Rules are immutable and content-addressed, so a rule edit is a pointer swap.** If the new
rule's fingerprint already exists, there is **no recompute at all** — the usual case, and the
entire return on content addressing. If it is new, one indexed scan materialises it.

Neither cost depends on how many tasks reference the rule. The naive alternative — recomputing
eligibility for the edited task — is the same work *every time* and scales with task count.

**Declared policy on an assignee who no longer qualifies:** a task still `todo` is released and
re-placed, because the author has just redefined who may do it and nothing has started. A task
already `in_progress` keeps its assignee and the mismatch is reported — discarding work in
flight is worse than a temporary mismatch.

---

## 5. Required APIs

| Required | Status | Measured p95 |
|---|---|---|
| `POST /tasks/` — create with rules | ✅ | — |
| `GET /tasks/{id}/eligible-users` — highly optimised | ✅ | **1.65 ms** |
| `GET /my-eligible-tasks` | ✅ | **0.38 ms** |
| `POST /tasks/recompute-eligibility` | ✅ | 202 + job ids |

Beyond the four: `GET`/`PATCH`/`DELETE /tasks/{id}`, `GET /tasks/`, `POST /tasks/{id}/complete`,
signup/login/refresh, `/schema`, `/docs`.

**Why recompute returns 202.** A full recompute at the stated scale is minutes of worker time.
A synchronous endpoint would either time out or lie about what it costs. It returns job ids and
is idempotent per rule — a rule already materialising is skipped by the single-flight lock, so
submitting twice does the work once.

---

## 6. Performance expectations

**Asked for:** 100k users, 1M tasks, APIs under 200 ms using caching, indexing and background
processing.

**Measured** on PostgreSQL 14 at exactly that scale — 100,000 users, 1,000,000 tasks,
R = 1,000, **15,384,332 eligibility rows (2380 MB)** — 8 concurrent workers, 400 iterations
after warmup:

| Query | p50 | p95 | achieved rate |
|---|---|---|---|
| `/my-eligible-tasks` | 0.38 ms | 1.46 ms | 8,778/s |
| `/tasks/{id}/eligible-users` | 0.89 ms | 1.65 ms | 4,991/s |
| assignment: top candidate | 0.26 ms | 0.68 ms | 14,423/s |
| assignment: next pooled task | 0.39 ms | 0.81 ms | 9,943/s |

**≈120× margin on the 200 ms budget.** `manage.py benchmark` *asserts* there is no sequential
scan on any request-path query rather than reporting it — a plan regression is invisible until
the table grows.

The rate is printed beside every latency because "under 200 ms" means nothing without a load
figure. ⚠️ These are **SQL-layer** measurements: they exclude WSGI, JSON serialisation and
network, so end-to-end HTTP is higher. Stated in the benchmark's own docstring.

**The three named mechanisms:**

- **Indexing** — §4 of the README. Notably `users_selection_order` (the ladder, walked not
  sorted) and partial indexes keyed on open work rather than lifetime volume.
- **Caching** — immutable rule specs, single-flight on materialisation, recompute debounce.
  The eligible-set cache was measured and dropped.
- **Background processing** — every assignment decision runs in a worker; nothing places a
  task on the request path.

**The row-count model was checked, not trusted:** predicted `R · d · U` =
`1000 × 0.1538 × 100,000 = 15,380,000`; actual **15,384,332**.

---

## 7. Deliverables

| Deliverable | Status | Notes |
|---|---|---|
| Public GitHub repository | ❌ **Not done** | Not yet a git repository. The only outstanding deliverable |
| Docker setup | ✅ verified | Five services healthy; see §8 |
| DB migrations | ✅ | 5 migrations, apply and reverse cleanly |
| README: architecture decisions | ✅ | D1–D17, each with its rejected alternative |
| README: indexing strategy | ✅ | §4, with `EXPLAIN` evidence and an operational reindex requirement |
| README: caching strategy | ✅ | §7, including what was dropped and why |
| README: rule engine design | ✅ | §3 |
| README: recompute strategy | ✅ | §5 |
| Seed data | ✅ | `seed` (demo, drives the real services) and `seed_scale` (benchmark, parameterised on R and d) |
| API documentation | ✅ | OpenAPI at `/schema`, Swagger UI at `/docs`, zero generation errors |

⚠️ **React UI is deliberately thin** — two screens: create a task with a rule builder, and see
your own work. It appears in none of the evaluation criteria, and README §13 says so from the
start rather than presenting it as complete. Its rule builder mirrors the closed predicate set
exactly, because there is no DSL to expose: the shape of that form *is* the shape of a rule.

---

## 8. Evaluation criteria

| Criterion | Where to look |
|---|---|
| **Architecture quality** | README D1–D17 — every decision with the alternative it beat. §11 lists what is *unresolved* and why, rather than papering over it |
| **Database design & indexing** | README §2 and §4. Stable/volatile split, content-addressed rules, `EXPLAIN`-verified plans, and the measured index-bloat requirement (SP-4) |
| **Performance optimisation** | README §6 and §10, plus §6 above. Measured at stated scale, with retractions where measurement contradicted the design |
| **Clean code and structure** | `services.py` holds the logic and is testable without a broker; views validate and delegate; raw SQL only where it must be exact (the ladder and the compare-and-set) |
| **Rule engine implementation** | `rules.py` — two evaluators, one exhaustive property test asserting they agree |
| **Background processing design** | One primitive (`fill_capacity`), five triggers, plus a backstop sweep for dropped messages. §6 |

---

## 9. Verification

**55 tests**, PostgreSQL 14. They cover the places where being wrong is invisible: the two
evaluators against each other, the priority-overtake race, the capacity race under 16-way
concurrency, drain ordering, recompute deltas, cache and lock failure modes, the CRUD
lifecycle, and counter release on delete. Not CRUD serialisers — those fail loudly and cheaply.

`docker compose up --build` verified: all five services healthy, seed runs in-container, API,
Swagger and UI all respond, worker processes placement through Redis, beat schedules both
safety nets.

**Four bugs were found by running the real stack that the test suite could not catch**, all
because `CELERY_TASK_ALWAYS_EAGER=1` runs jobs inline and erases the concurrency production
has:

1. `drf-spectacular` missing from `requirements.txt` — the image would not boot.
2. The create response reported a definitive reason for a pending outcome.
3. Tasks with a brand-new rule pooled and stayed pooled — materialisation and placement are
   unordered jobs, and materialisation was missing from the trigger set.
4. Seeded users had no usable password, making "My tasks" impossible to demo.

Each now has a regression test forcing the adversarial ordering. README §12 is the full ledger
of what is measured versus reasoned.
