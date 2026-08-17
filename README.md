# Dynamic Rule-Based Task Assignment

A task management system where tasks are never assigned by hand. Each task carries a rule
describing who may do it; the system computes eligible users and assigns the task in the
background, in priority order.

**Stack:** Django + DRF, PostgreSQL, Redis, Celery, Docker Compose. React for a thin admin UI.

> **This is an interview exercise, not a production deployment.** Every credential in the
> repository is a deliberate placeholder: `manager/manager`, `admin/admin`, `userNNNN/demo`,
> a Postgres password that never leaves the compose network, and a `SECRET_KEY` default of
> `dev-only-not-for-production`. All are overridable by environment variable, and §13 lists
> the security simplifications (notably JWTs in `localStorage`) with what production would
> use instead.

**Requirement-by-requirement traceability:** [REQUIREMENTS.md](REQUIREMENTS.md)
**Delivery plan, backlog and board:** [ROADMAP.md](ROADMAP.md)
**Evidence:** §12 lists exactly what has been measured and what has not.

## Running it

```bash
docker compose up --build
```

| | |
|---|---|
| UI | http://localhost:5173 |
| API docs (Swagger) | http://localhost:8000/docs |
| OpenAPI schema | http://localhost:8000/schema |

Logins after seeding: `manager/manager`, `admin/admin`.

Without Docker, against a local PostgreSQL:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
createdb taskassign
POSTGRES_HOST=/tmp POSTGRES_PORT=5432 POSTGRES_DB=taskassign \
  .venv/bin/python manage.py migrate
.venv/bin/python manage.py seed --users 200 --rules 8 --tasks 50
.venv/bin/python manage.py test assignment
```

Celery runs eagerly in-process unless `CELERY_EAGER=0`, so the test suite and a
bare `runserver` exercise the whole assignment path without a broker.

**Verified, including `docker compose up --build`:** all five services build and come up
healthy, the seed runs in-container, the API, Swagger docs and UI all respond, and the Celery
worker with beat processes placement and schedules the safety nets. Plus the 42-test suite,
the scale benchmark, and the UI flow end to end.

Running the real stack found two bugs that native execution could not:

- **`drf-spectacular` was missing from `requirements.txt`** — pip-installed locally but never
  pinned, so the image failed to boot. Requirements are now frozen from the venv.
- **The create-task response reported a false reason.** With a real broker, `place_task` is
  queued and has *not* run when the response is built, so a task the worker assigned a second
  later came back claiming "43 users match, all at capacity". Eager test settings hid this
  completely — placement finishes inline before serialisation. It now reports `pending` and a
  unit test pins both branches.

---

## Decisions at a glance

Every decision is stated with its reason and the alternative it beat. Detail follows in the
numbered sections.

| # | Decision | Why | Rejected |
|---|---|---|---|
| D1 | Rules are content-addressed and deduplicated | Eligibility is a property of the rule, not the task; identical rules should be computed once | Per-task eligibility |
| D2 | Predicates split stable vs. volatile | Load changes on every assignment; materialising it means permanent recompute | Uniform treatment of all predicates |
| D3 | Rules are a flat AND of four optional predicates | The attribute set is closed; a DSL buys expressiveness nothing needs, and unbounded rule cardinality would break D1 | General boolean expression language |
| D4 | Rule rows are immutable | Makes a rule edit a pointer swap and makes caching them trivially safe | Mutable rules with versioning |
| D5 | Recompute-on-user-change scans cached rules | ~10³ in-memory predicate evals beats index machinery at this cardinality | Inverted index over predicates |
| D6 | **Capacity is capped by task count, never by effort hours** | A count cap means every eligible user can take any task, so placement is always feasible. An hours cap makes it bin packing, and tasks strand while capacity sits free | `max_effort_hours` predicate |
| D7 | **Effort hours order selection but gate nothing** | "Least loaded" is meaningless if one 8-hour task equals four 30-minute ones | Task count as the load proxy |
| D8 | User selection is a four-key deterministic ladder | Least current load, then track record, then seniority, then a unique key for a total order | Random or round-robin tie-break |
| D9 | Capacity claimed by compare-and-set | Read-then-write races two workers past the cap | Advisory locks, `SELECT FOR UPDATE` |
| D10 | **One assignment primitive, four triggers** | Two paths let a newly created P2 overtake a waiting P0 — reproduced in `tests/` | Direct assign on creation + drain on completion |
| D11 | Task selection is `priority ASC, created_at ASC` | P0 first; FIFO within a band bounds waiting time | Due-date or effort ordering |
| D12 | Priority orders the queue; it does not preempt | A P0 with no eligible user waits like any task, then wins the first freed slot | Preemption, or reserved headroom |
| D13 | Unmatched tasks enter a pool, retried on events | A rule matching nobody today is a timing problem, not an error | Reject the task, or poll on a cron |
| D14 | Cache the stable set, never the load counters | A cache stale on load hands out cap-violating assignments | Cache the whole eligible-user result |
| D15 | Two evaluators (SQL + Python), one property test | The two query directions have opposite shapes; one generic engine fits neither | Single evaluator forced to serve both |
| D16 | Capacity strongly consistent, eligibility eventually | Capacity is a real constraint; eligibility is advisory and self-correcting | Uniform consistency either way |
| D17 | Full recompute is async, returns 202 | It is minutes of worker time; a synchronous API would lie about its cost | Synchronous recompute endpoint |

---

## 1. The actual problem

### What is known, and what is not

Stated in the brief:

```
U = 100,000      users        (total)
T = 1,000,000    tasks        (total)
    4            departments  (Finance, HR, IT, Operations)
    active_tasks < N          the capacity rule
```

Not stated, and not derivable from the brief:

```
R = number of distinct rules across those T tasks
d = fraction of users a given rule matches  ("selectivity")
```

`R` and `d` are properties of how admins actually author rules. Nothing in the brief
constrains either. They are carried as symbols throughout rather than replaced with guesses;
§11 states how each is handled.

`U` and `T` are totals, not a multiplier. Neither is difficult alone — Postgres does not
notice a 100k-row table, and 1M task rows is ordinary. The cost is in the *relation* between
them: "which users are eligible for this task" is a many-to-many that must stay correct as
both sides change.

### The size of that relation

```
per-task materialisation :  rows  =  T · d · U
per-rule materialisation :  rows  =  R · d · U
                                     ───────────
                            saving  =  T / R
```

`d` and `U` cancel. **The case for content-addressing rules rests on exactly one question —
do rules repeat? — and needs no selectivity estimate whatsoever.** That is why D1 carries no
number.

On upper bounds: the only true ceiling on this relation is `T · U = 10¹¹` pairs, reached only
at `d = 1` — every rule matching every user. No real rule set does that, so the bound is
correct and useless. Every figure below it is a point estimate requiring a value for `d`, and
is labelled as such wherever it appears. Absolute sizing lives in §10, where `d` is varied
rather than assumed.

### Observation 1 — eligibility belongs to the rule, not the task

A rule is a small value: a department set, an experience threshold, a location set, a task
cap. Two tasks carrying the same rule have byte-identical eligible sets — definitional, not
an estimate.

So eligibility is materialised **per distinct rule**: rules are canonicalised, hashed, and
deduplicated, and the eligible set is computed once per fingerprint. The saving is `T / R`.

Whether that saving is large is exactly the question of whether `R ≪ T`. That is a hypothesis
about admin behaviour, not a fact, and the build measures it (SP-1) before the caching layer
commits to a shape. §10 states behaviour across the full range of `R`, including the
degenerate case `R = T` where per-rule materialisation collapses into per-task materialisation
exactly.

### Observation 2 — the predicates have wildly different volatility

| Field | Role | Changes when | Frequency |
|---|---|---|---|
| Department | rule predicate | HR event | Rare |
| Experience (years) | rule predicate | HR event / anniversary | Rare |
| Location | rule predicate | HR event | Rare |
| **`active_task_count`** | **the capacity cap** | **every assignment and completion** | **Constant** |
| **`committed_effort_hours`** | **selection key 1** | **every assignment and completion** | **Constant** |
| **`lifetime_hours`** | **selection key 2** | every completion | Frequent |

This is the trap in the brief. `active_tasks < 5` changes on every single assignment. If it
participates in materialised eligibility, one assignment invalidates every rule set the
assignee belongs to, and the system spends its life recomputing itself.

So the rule is split at evaluation time:

- **Stable predicates** (department, experience, location) → materialised, cached,
  recomputed in the background.
- **Volatile fields** (the three above) → never materialised. Applied as a `WHERE` clause or
  an `ORDER BY` at query time against columns on `users`.

**Why this matters:** the fields generating the most write traffic generate *zero* background
work. The caching rule in §7 and the recompute costs in §5 both fall out of this split.

**Evidence the boundary is in the right place:** adding two selection dimensions
(`committed_effort_hours`, `lifetime_hours`) later in the design required no change to
`rule_eligible_user` at all. They are volatile, so they never entered the fingerprint.

---

## 2. Data model

```
users
  id, email, password_hash, role         -- admin | manager | user
  department             smallint        -- Finance, HR, IT, Operations   } stable
  experience_years       smallint        --                               } stable
  location               text            --                               } stable
  active_task_count      int             -- count of non-done assigned    } volatile: CAP
  committed_effort_hours numeric(7,2)    -- Σ effort of non-done assigned } volatile: KEY 1 asc
  lifetime_hours         numeric(9,2)    -- Σ effort of DONE tasks, ever  } volatile: KEY 2 desc
  created_at             timestamptz     -- account age                     KEY 3 asc
  stable_attrs_version   int             -- bumped when a stable field changes

rules                                    -- immutable, content-addressed
  id
  fingerprint            text unique     -- sha256 of canonicalised STABLE predicates
  predicates             jsonb           -- stable predicates only
  max_active_tasks       int null        -- volatile, deliberately outside the fingerprint
  eligible_count         int null        -- |materialised set|; selects the query plan, §6
  materialized_at        timestamptz null

rule_eligible_user                       -- stable eligibility only
  rule_id, user_id                       -- PK (rule_id, user_id)

tasks
  id, title, description, due_date
  priority               smallint        -- 0 = P0 (highest), 1 = P1, 2 = P2, …
  effort_hours           numeric(6,2)    -- NOT NULL; size of the task
  status                 text            -- todo | in_progress | done | cancelled
  rule_id                -- FK rules
  created_by             -- FK users, NOT NULL; the Manager who authored it
  assignee_id            -- FK users, nullable; NULL = in the unassigned pool
  created_at             timestamptz

notifications                            -- written in the completion transaction
  id
  user_id                -- FK users; the recipient (the task's created_by)
  task_id                -- FK tasks
  kind                   text            -- task_completed | task_stuck
  created_at             timestamptz
  read_at                timestamptz null
```

**Why `created_by` exists:** a Manager authors a task and is notified when it is done, so the
task has to remember who to tell. The brief has no such field — it models tasks as ownerless
once created.

**Why notifications are a table written in the completion transaction, not a queued job.**
Enqueueing a Celery job to send the notice is the obvious approach and it is wrong: the job
can be picked up before the transaction commits (reading a task that is not yet done), or the
transaction can commit while the enqueue fails, losing the notice entirely. Writing the row
inside the same transaction makes the notice exactly as durable as the completion that caused
it. A worker then delivers unread rows — the transactional outbox pattern, in its cheapest
possible form.

**Declared out of scope:** the delivery *channel*. The table is the source of truth and the
API exposes unread rows for polling. Email, websocket, or push is a delivery concern layered
on top and is not built.

**Why `priority` is `smallint`, not a text enum (D11):** drain order is `ORDER BY priority
ASC`. Text sorts lexically, so `'P10'` would sort before `'P2'` — silently wrong the moment
priorities pass single digits. An integer sorts correctly and indexes tightly.

**Why `numeric`, not `float`, for effort (D7):** `committed_effort_hours` is a running sum,
incremented on every assignment and decremented on every completion. Binary floating point
cannot represent 0.1 exactly, so those repeated ± operations accumulate drift. Since the
column is selection key 1, drift silently corrupts assignment ordering — and unlike a cap
breach, nothing ever surfaces it. `numeric` is exact.

**Effort orders; it does not cap (D6, D7).** The only capacity limit is the brief's own
`active_tasks < N`. `effort_hours` exists so "least loaded" is a meaningful comparison — one
8-hour task versus four 30-minute ones — and is used solely in the selection ordering (§6).
There is no `max_effort_hours` predicate.

**Why that distinction carries more weight than it looks.** Capping on hours would make
placement a bin-packing problem: a 6-hour task could not go to a user with 3 hours of
headroom, capacity would fragment, and tasks would strand while aggregate capacity sat free —
provably, since online bin packing cannot know what is coming. Capping on task *count* means
every eligible user can take every task whatever its size. Placement stays trivially feasible
and the completeness guarantee in §6 stays strong. Ordering by effort buys the fairness;
capping by effort would have bought a hard problem.

**Why `active_task_count` is denormalised (D2):** it is read on every candidate query and
written on every assignment. Deriving it with `COUNT(*)` over `tasks` puts an aggregate on the
hot path, and — more importantly — makes the atomic claim in §6 impossible. The cost is that
it can drift, so it is only ever written inside the assignment/completion transaction, and a
reconciliation job verifies it against `tasks`.

**Why `max_active_tasks` sits outside `predicates` (D2):** it must not enter the fingerprint,
or two rules differing only in their cap would materialise separate identical eligible sets.
Held apart, those rules share one materialised set and differ only in a query-time filter.

**Why rules are immutable (D4):** "editing a task's rules" never mutates a rule row — it
repoints the task at a different `rule_id`, creating that rule only if its fingerprint is new.
A rule edit becomes a pointer swap rather than a cascading invalidation, rule rows are safe to
cache forever with no invalidation logic, and Story 4's "recompute efficiently" is usually *no
recompute at all*. The cost is orphaned rule rows, collected by a periodic job.

### Rule shape

The API accepts one flat object; the server splits it on the stable/volatile line before
hashing:

```json
{
  "department":       "Finance",     // stable  ─┐
  "experience_years": { "gte": 4 },  // stable   ├─ fingerprinted
  "location":         "Bangalore",   // stable  ─┘  optional: omit for anywhere
  "max_active_tasks": 5              // volatile — stored beside, not inside
}
```

A flat AND of at most four optional predicates, each **single-valued**. A rule names one
department and at most one location: no nesting, no OR anywhere — not across fields and not
within one. A one-element list is accepted and flattened for older clients; more than one
value is **rejected, not truncated**, because a rule that quietly dropped a department would
route work to the wrong team with nothing to show for it.

**Single-valued predicates also shrink `R`,** which is the quantity D1's leverage depends on:
the reachable rule space is now `departments × experience bands × (locations + 1)` instead of
its powerset.

**Why not a rule DSL (D3):** the attribute set is closed and known. A general expression
language means a grammar, a parser, an AST, an evaluator, and a safety story — bought to
express predicates that fit in a struct. It would also break D1: arbitrary expressions have
unbounded cardinality, so dedup stops working and the design's leverage disappears. The
compiler in §3 is ~40 lines. Stated ceiling: if arbitrary boolean nesting is ever genuinely
needed, the migration is a rule tree with the *same* stable/volatile split — the split is the
load-bearing idea, not the flat shape.

**Why canonicalise before hashing:** sort keys, sort and dedupe lists, drop null and empty
predicates, normalise numeric types. `{"department": ["Finance"]}` and
`{"department": ["Finance"], "location": []}` must produce the same fingerprint, or dedup
degrades toward per-task materialisation without anything failing loudly.

---

## 3. Rule engine

Two evaluators over the same canonical JSON:

```python
def to_sql(predicates) -> tuple[str, list]:
    """Rule -> WHERE clause. One rule against 100k users: a database scan."""

def matches(predicates, user) -> bool:
    """Rule -> in-memory predicate. One user against ~1k rules: a loop."""
```

**Why two (D15):** the problem runs in both directions and they have opposite shapes.
Materialisation is one rule against every user — that belongs in the database behind an index.
Recompute-on-user-change is one user against every rule — that belongs in a loop over cached
rule rows and never touches the database. A single generic engine forced to serve both would
either drag 100k users into Python or issue 1k queries per user edit.

**Why that is safe:** two implementations of one semantics is a real divergence risk, so it
carries the one test that matters — a property test asserting that for a seeded population,
`to_sql` and `matches` select the identical user set across generated rules. If it passes, the
duplication is not a liability.

---

## 4. Indexing strategy

```sql
-- materialisation: one rule -> its eligible users
CREATE INDEX ON users (department, experience_years) INCLUDE (location);

-- user selection, in ladder order: lets Postgres WALK the order and stop at LIMIT n
-- rather than collecting the eligible set and sorting it (§6, measured)
CREATE INDEX users_selection_order ON users
  (committed_effort_hours ASC, lifetime_hours DESC, created_at ASC, id ASC);

-- forward: eligible users for a rule
ALTER TABLE rule_eligible_user ADD PRIMARY KEY (rule_id, user_id);

-- reverse: rules a user is eligible for -- drives recompute_user diffs (§5)
--          and the fill_capacity pool query (§6)
CREATE INDEX ON rule_eligible_user (user_id, rule_id);

-- the unassigned pool, in DRAIN order (§6 [1]). Note ASC on priority: 0 = P0 first.
CREATE INDEX ON tasks (rule_id, priority ASC, created_at ASC)
  WHERE assignee_id IS NULL AND status <> 'done';

-- a user's own work, in the order /my-eligible-tasks returns it (§8)
CREATE INDEX ON tasks (assignee_id, priority ASC, due_date ASC) WHERE status <> 'done';

CREATE UNIQUE INDEX ON rules (fingerprint);
```

**Why `priority ASC` and not `DESC`:** P0 is the *highest* priority and the *lowest* integer.
An index built `DESC` would serve the drain backwards — P-highest last. This is the kind of
error that produces a working system that assigns exactly wrong.

**Why `rule_eligible_user` is indexed both ways:** the same table answers both directions of
§3. `(rule_id, user_id)` serves "who can do this task"; `(user_id, rule_id)` serves "what can
this user do". Two indexes on a few-million-row table is cheap; a second denormalised table
would be a second thing to keep correct.

**Why the partial indexes:** `WHERE status <> 'done'` keeps the hot indexes proportional to
*open* work rather than lifetime task volume. Over 1M tasks with most completed, that is the
difference between an index that stays resident in cache and one that does not.

**`users_selection_order` earns its place, and carries an operational cost that must be
scheduled.** The planner drives the candidate query from this index (§6). But its leading
column is volatile, so every assignment relocates an index entry, and a B-tree cannot reuse
the vacated space the way the heap can:

| After | Index | Heap |
|---|---|---|
| baseline (2,000 users) | 216 kB | 352 kB |
| 200k assignment-shaped updates | 12 MB | 22 MB |
| 400k updates | **23 MB** | 22 MB |
| `VACUUM` | no change | no change |
| `REINDEX` | **112 kB** | — |

The heap plateaus — `VACUUM` returns space to the free space map and subsequent updates reuse
it. **The index does not.** It grew roughly linearly, 12 MB to 23 MB across the second 200k
updates, and `VACUUM` reclaimed none of it. `REINDEX` restored it to 112 kB, half a percent of
the bloated size.

**Operational requirement, not a nice-to-have:** `REINDEX INDEX CONCURRENTLY
users_selection_order` on a schedule, plus monitoring of index size against row count. Without
it this degrades silently — nothing errors, queries just get slower as the index stops fitting
in cache. At the human-paced write rate this design assumes, quarterly is likely enough; the
trigger should be measured size, not a guess at cadence.

**Why materialisation is deliberately not optimised:** it scans users behind the composite
index and takes tens of milliseconds at 100k rows. It runs in a worker, off the request path.
Nobody is waiting on it. Knowing what not to optimise is part of the design.

**Planner setting:** `random_page_cost = 1.1`. The 4.0 default models a seek-bound spinning
disk and makes Postgres prefer sequential scans over index probes; on SSD that is wrong and
cost the narrow-rule query 2× (§6).

---

## 5. Recompute strategy

Three events change eligibility. Each has a different, bounded cost.

### A task is created, or a task's rule changes (Stories 1 and 4)

```
1. canonicalise + hash the incoming rule
2. INSERT ... ON CONFLICT DO NOTHING into rules; take the id
3. rule exists and is materialised  -> NO recompute at all
4. rule is new                      -> enqueue materialize_rule(rule_id)
5. insert the task into the pool (assignee_id NULL)
   and enqueue fill_capacity(u) for its rule's single top-ranked candidate
```

**The task is never assigned directly.** Step 5 routes creation through the same pool drain as
every other trigger; §6 explains why that is required, not merely tidy.

**Why this answers Story 4:** the common case for a rule edit is zero work, because admins
reuse rules. The worst case is one scan of a 100k-row table. Neither cost depends on how many
tasks reference the rule — the entire return on content-addressing (D1). The naive
alternative, recomputing eligibility for the edited task, is the same work *every time* and
scales with task count.

### A user's stable attributes change (Story 3)

Only stable attributes trigger anything. A change to `active_task_count`,
`committed_effort_hours`, or `lifetime_hours` triggers nothing, by construction (D2).

`recompute_user(user_id)` evaluates the user against every rule in memory, diffs the result
against that user's current rows in `rule_eligible_user`, and writes only the delta. If the
delta *adds* rules, `fill_capacity(user_id)` is enqueued — the user may now be able to take
work that was waiting.

**Why not an inverted index over predicates (D5):** testing one user against ~10³ cached rules
is a microsecond-scale loop. An inverted index would be more code, more invalidation surface,
and *slower* at this cardinality — it optimises a scan that is already free. The scaling lever
if `R` ever passes ~10⁵: bucket rules by department, which is always present and most
selective, and test only the matching bucket. Same code path, one added filter.

**Why the delta, not a rewrite:** deleting and re-inserting a user's rows churns the index and
makes "what changed" invisible. The diff makes newly-gained rules an explicit event — which is
what re-queues pooled tasks, and therefore what answers "no eligible users existed at creation
time" (D13).

Rapid successive edits are debounced through a short-TTL Redis key, so a burst of profile
saves produces one recompute rather than five.

### A task is completed

All of this is one transaction:

```
tasks.status            = 'done'
active_task_count      -= 1                  slot freed
committed_effort_hours -= task.effort_hours  } effort moves from
lifetime_hours         += task.effort_hours  } current load to track record
INSERT notifications (user_id = task.created_by, kind = 'task_completed')
                                             ^ same transaction, see §2
-- then, after commit:
enqueue fill_capacity(assignee)
```

No eligibility recompute — the stable predicates did not change. Only the pool drain runs.

**Cancellation is not completion.** A cancelled task frees the slot and decrements
`committed_effort_hours`, but must **not** credit `lifetime_hours` — no work was delivered,
and crediting it would corrupt selection key 2, silently and permanently. The creator is
still notified, with a different `kind`.

**Why event-driven rather than a cron (D13):** polling the pool trades latency against wasted
scans, and every interval setting is wrong somewhere. The events that can change the answer
are known and few, so they trigger directly. A low-frequency sweep runs as a backstop for
anything a dropped queue message missed — a safety net for infrastructure failure, not the
mechanism.

---

## 6. Assignment

### Two orderings, independent of each other

```
[1] TASK selection  — many pooled tasks, scarce capacity: which task next?
        ORDER BY priority ASC, created_at ASC

[2] USER selection  — one task, many eligible users: who gets it?
        ORDER BY committed_effort_hours ASC, lifetime_hours DESC,
                 created_at ASC, id ASC
```

Conflating these is a common error. [1] is a queue discipline over work; [2] is a placement
policy over people. Neither constrains the other.

### [1] Task selection — priority, then age

**Why age within a band (D11):** without a second key the order is arbitrary, and a task can
be passed over indefinitely while newer siblings of equal priority overtake it — starvation.
`created_at` makes each band a queue, so waiting time is bounded by the arrival rate of
higher-priority work.

**Priority orders; it does not preempt (D12).** A P0 arriving when every eligible user is at
cap waits in the pool like any other task. It is not rejected, and it does not displace work
already in progress. The moment capacity appears, it is assigned first. Priority governs queue
position, not the right to evict.

**What makes that guarantee real** is §6's single primitive, not the `ORDER BY` alone. See
below — with two assignment paths the guarantee silently fails.

### [2] User selection — the four-key ladder

| # | Key | What it expresses | Fails without it |
|---|---|---|---|
| 1 | `committed_effort_hours ASC` | Fairness of *current* burden, measured in hours so one 8-hour task is not equal to four 30-minute ones | — this is the policy, not a tie-break |
| 2 | `lifetime_hours DESC` | Track record. Among equally-burdened users, prefer proven throughput | Equally-loaded users chosen arbitrarily |
| 3 | `created_at ASC` | Seniority of account | Ties among users with identical load and lifetime |
| 4 | `id ASC` | — | **The order is not total.** `created_at` is not unique (a bulk import stamps many rows identically), so without a unique final key equal rows return in engine-chosen order: irreproducible tests, and in practice the same row repeatedly |

Keys 1 and 2 pull in opposite directions deliberately: least *current* load, most *lifetime*
hours. The first distributes today's burden; the second breaks ties toward people who have
demonstrably delivered.

**Key 4 is an addition, declared.** It is not in the specified ladder, but determinism is the
stated goal and `created_at` alone cannot deliver it. `id` is unique, so the sort is a total
order and identical state always yields an identical choice.

### What the ladder does and does not guarantee

```
determinism   (the ladder)     same state -> same choice, always
completeness  (the drain loop) every placeable task eventually placed
```

Only the first belongs to the ordering. Completeness comes from the pool being drained on the
complete set of triggering events.

**Completeness, stated precisely.** For a pooled task `T` with rule `R`, if there exists a
user `U` with

```
U ∈ materialised_eligible(R)              -- stable predicates hold
U.active_task_count < R.max_active_tasks  -- has a free slot
```

then `T` is assigned, subject to [1] — higher-priority, or equal-priority-earlier, tasks
consume free slots first.

**This guarantee is strong precisely because the cap counts tasks rather than hours (D6).**
Every eligible user with a free slot can take *any* task regardless of size: no fit problem,
no fragmentation, no task stranded while capacity sits free elsewhere.

### One assignment primitive, four triggers

Priority ordering only holds if there is a **single** place where assignment decisions are
made. The two-path design this replaces — a direct `assign_task(task_id)` on creation plus a
`fill_capacity(user_id)` on completion — silently breaks the P0 guarantee:

```
P0 sits in the pool; every user eligible for its rule is at cap.

t0  user U completes a task        -> slot free, enqueue fill_capacity(U)
t1  admin creates a P2 task        -> enqueue assign_task(P2)
t2  worker picks up assign_task(P2) first
        -> U is eligible and has a free slot -> P2 assigned to U
t3  fill_capacity(U) runs          -> no capacity -> P0 still waiting

                          P2 overtook P0.
```

The direct path never consulted the pool, so it could not know a higher-priority task was
already waiting for that slot. **This is reproduced as a failing case in
`tests/test_s7_priority_overtake.py`, which then proves the fix.**

The fix is to delete the direct path. One primitive:

```
fill_capacity(user_id):
    loop:
        take the highest-ranked pooled task this user is eligible for
             ORDER BY priority ASC, created_at ASC   LIMIT 1
        claim it atomically; stop when the claim fails or nothing is left
```

| Trigger | Enqueues |
|---|---|
| Task completed | `fill_capacity(assignee)` |
| User's stable attributes changed (gaining rules) | `fill_capacity(user)` |
| User created | `fill_capacity(user)` |
| **Task created** | `fill_capacity(u)` for its rule's single top-ranked candidate (`LIMIT 1`; re-queried on a lost race) |
| **A rule finishes materialising** | drains that rule's pooled tasks in priority order |

**Why task creation goes through the pool too.** A new task is not assigned directly; it enters
the pool and its rule's best candidates are asked to fill. Each of those users drains *their*
pool in priority order, so the new task is taken only if it is the highest-ranked thing that
user could have taken. A P0 waiting on the same user always wins. This makes "P0 gets assigned
first" structural rather than a matter of which Celery job happened to run first.

**Cost of routing creation through the pool: none.** Direct assignment was already a background
job. This is the same single worker hop, with the pool consulted instead of ignored — and one
primitive instead of two.

**Why the primitive is keyed on the user, not the task:** every trigger is really the event
"this user has room now". Asking which pooled tasks that user can take is one indexed query.
Asking which of a million tasks might now be placeable is not.

**Why it loops.** One completion frees one slot, so it usually assigns one task. But a newly
created user, or one whose attributes just made them eligible for several rules, may have their
whole cap free at once — the loop fills it rather than leaving slots idle until unrelated
events trickle in.

**Materialisation is a trigger too — this was missing and it cost a real bug.** Task creation
enqueues `materialize_rule` and `place_task` as independent jobs. Nothing orders them, so for
any brand-new rule placement routinely ran first, found an empty eligibility table, pooled the
task, and nothing retried it but the five-minute sweep. Verified against the running stack: 27
eligible users all at zero load, task still unassigned. Materialisation now drains its own
rule's pool, bounded, with the sweep covering any remainder.

The eager test settings hid it completely — they ran materialisation to completion first every
time. It is the second bug of exactly that shape, after the false "all at capacity" response.
Convenient test settings erase the concurrency the real system has.

**Residual race, declared.** Two `fill_capacity` jobs for *different* users can each claim a
different slot concurrently; that is correct and wanted. What remains theoretically open is
cross-rule ordering at the exact instant of release. Closing it fully needs a global assignment
lock, which serialises all assignment to buy a guarantee the pool already delivers on the next
release. Not built. Named rather than papered over.

### The claim must be atomic

The cap is checked inside the write, so it cannot be raced past. `committed_effort_hours` rides
along as bookkeeping for the ordering — updated, never gated on:

```sql
UPDATE users
   SET active_task_count      = active_task_count + 1,
       committed_effort_hours = committed_effort_hours + %(effort)s
 WHERE id = %(user_id)s
   AND (%(max_tasks)s::int IS NULL OR active_task_count < %(max_tasks)s)
RETURNING id;
```

Zero rows returned means another worker took the slot; move to the next candidate. The counter
update and the `tasks.assignee_id` write share a transaction.

**Why compare-and-set (D9):** reading a candidate and then assigning is a race — two concurrent
workers both see `active_task_count = 4` against a cap of 5, both assign, and the user lands at
6. The predicate must be evaluated *inside* the write. Postgres locks the row for the duration
of the `UPDATE`, so the loser blocks, re-reads the new value, re-evaluates its `WHERE`, and
matches zero rows. `SELECT FOR UPDATE` would also be correct but serialises candidate selection
and introduces lock ordering to get wrong; the CAS needs neither, and its failure mode is a
cheap retry against the next candidate.

**Verified:** 16 barrier-synchronised workers racing for one slot — exactly one won, fifteen
lost the race, the cap held, zero errors, stable across five runs. The test fails if *nobody*
loses, since that would mean contention never occurred and the run proved nothing.

### No eligible users

The task is created and left in the pool — not rejected, not errored — and picked up by the §5
triggers the moment someone becomes eligible.

**Why not reject (D13):** a rule matching nobody *today* is a timing problem, not a validation
error. The new hire arrives Monday; the current assignee finishes at 4pm. Losing the task at
creation forces the admin to poll and re-create.

Two causes are distinguished, because they need different human responses:

```
COUNT(*) FROM rule_eligible_user WHERE rule_id = X

  = 0  ->  STRUCTURAL: no user satisfies the stable predicates.
           May never resolve. The admin must fix the rule.
           API: "no user matches this rule"

  > 0  ->  TRANSIENT: users match; all are at their task cap.
           Resolves on the next completion. Nothing to do.
           API: "N users match, all at capacity"
```

The discriminator is free — it is `rules.eligible_count`, which materialisation already
maintains.

**Aging.** A task in the structural case can sit indefinitely, and a task nobody can see is
indistinguishable from one that was never created. Past an age threshold it is flagged for the
admin. The threshold is a declared operational value; the correct behaviour is to surface it,
never to auto-delete or auto-relax the rule.

### There is no candidate limit

The query takes `LIMIT 1`. The ladder is a total order, so it yields exactly one winner —
truncating to an arbitrary top-N was a number with no basis, and it is gone.

On a lost claim race the query is simply **re-run**. That is better than holding a candidate
list: a cached list of N goes stale as other workers claim slots, whereas re-querying always
reflects current state, and the winner's incremented counters naturally move them down the
ladder.

**Why this terminates.** Each lost race means another worker consumed a slot, so the set of
claimable users shrinks; the loop exits when the query returns nothing. A concurrent
*completion* can add a slot back, so the loop is not bounded in theory — but at manual task
creation rates, contention is effectively zero, and a task that falls through to the pool is
retried by the §5 triggers anyway. Self-correcting either way.

### Measured: the request path at full stated scale

PostgreSQL 14, **100,000 users, 1,000,000 tasks, R = 1,000, 15,384,332 eligibility rows
(2380 MB)**, measured `d` from 0.058 to 0.250. Real Django schema, real indexes. SQL layer
only — this excludes WSGI, JSON serialisation and network, so end-to-end latency is higher.

| Query | p50 | p95 | max | achieved rate |
|---|---|---|---|---|
| `/my-eligible-tasks` | 0.38 ms | 1.46 ms | 2.63 ms | 8,778/s |
| `/tasks/{id}/eligible-users` | 0.89 ms | 1.65 ms | 7.20 ms | 4,991/s |
| assignment: top candidate | 0.26 ms | 0.68 ms | 1.54 ms | 14,423/s |
| assignment: next pooled task | 0.39 ms | 0.81 ms | 1.23 ms | 9,943/s |

8 concurrent workers, 400 iterations each after a 20-iteration warmup. The rate is printed
beside every latency because "under 200 ms" means nothing without one.

**P-3 passes: no sequential scan on any request-path query.** The benchmark asserts this
rather than reporting it — a plan regression is invisible until the table grows.

Against the 200 ms budget the margin is roughly 120×. That is not a claim of headroom under
arbitrary load; it is what these four queries cost at this scale and this concurrency.

**Correction: `random_page_cost` made no measurable difference here.** An earlier benchmark on
a synthetic schema showed 18.2 → 9.3 ms from setting it to 1.1. On the real schema the same
change moved p95 from 1.67 to 1.65 ms — inside noise. The earlier result was an artefact of a
schema that lacked these indexes; with the right indexes the planner picks nested loops
either way. The setting is kept because 4.0 still models the wrong storage, but it is no
longer load-bearing and the earlier figure should not be quoted.

**The four-key ladder remains free.** On the same synthetic schema a single-key round-robin
ordering measured 24.7 ms against the ladder's 31.9 ms — 7 ms of a 32 ms query, almost all of
it row width. Round-robin is rejected on evidence: it buys nothing on a query whose cost lay
in the join plan, and forfeits the least-load property to do it.

**Key 4 is not decorative — measured.** On the synthetic dataset 17 users tied the winner on
keys 1–3 (same committed hours, same lifetime hours, same account-creation timestamp).
Without `id` as a final key, which of those 17 gets the task is whatever the engine returns
that day.

### The claim must be atomic

The cap is checked inside the write, so it cannot be raced past. `committed_effort_hours` rides
along as bookkeeping for the ordering — updated, never gated on:

```sql
UPDATE users
   SET active_task_count      = active_task_count + 1,
       committed_effort_hours = committed_effort_hours + %(effort)s
 WHERE id = %(user_id)s
   AND (%(max_tasks)s::int IS NULL OR active_task_count < %(max_tasks)s)
RETURNING id;
```

Zero rows returned means another worker took the slot; move to the next candidate. The counter
update and the `tasks.assignee_id` write share a transaction.

**Why compare-and-set (D9):** reading a candidate and then assigning is a race — two concurrent
workers both see `active_task_count = 4` against a cap of 5, both assign, and the user lands at
6. The predicate must be evaluated *inside* the write. Postgres locks the row for the duration
of the `UPDATE`, so the loser blocks, re-reads the new value, re-evaluates its `WHERE`, and
matches zero rows. `SELECT FOR UPDATE` would also be correct but serialises candidate selection
and introduces lock ordering to get wrong; the CAS needs neither, and its failure mode is a
cheap retry against the next candidate.

**Verified:** 16 barrier-synchronised workers racing for one slot — exactly one won, fifteen
lost the race, the cap held, zero errors, stable across five runs. The test fails if *nobody*
loses, since that would mean contention never occurred and the run proved nothing.

### No eligible users

The task is created and left in the pool — not rejected, not errored — and picked up by the §5
triggers the moment someone becomes eligible.

**Why not reject (D13):** a rule matching nobody *today* is a timing problem, not a validation
error. The new hire arrives Monday; the current assignee finishes at 4pm. Losing the task at
creation forces the admin to poll and re-create.

Two causes are distinguished, because they need different human responses:

```
COUNT(*) FROM rule_eligible_user WHERE rule_id = X

  = 0  ->  STRUCTURAL: no user satisfies the stable predicates.
           May never resolve. The admin must fix the rule.
           API: "no user matches this rule"

  > 0  ->  TRANSIENT: users match; all are at their task cap.
           Resolves on the next completion. Nothing to do.
           API: "N users match, all at capacity"
```

The discriminator is free — it is `rules.eligible_count`, which materialisation already
maintains.

**Aging.** A task in the structural case can sit indefinitely, and a task nobody can see is
indistinguishable from one that was never created. Past an age threshold it is flagged for the
admin. The threshold is a declared operational value; the correct behaviour is to surface it,
never to auto-delete or auto-relax the rule.

### There is no candidate limit

The query takes `LIMIT 1`. The ladder is a total order, so it yields exactly one winner —
truncating to an arbitrary top-N was a number with no basis, and it is gone.

On a lost claim race the query is simply **re-run**. That is better than holding a candidate
list: a cached list of N goes stale as other workers claim slots, whereas re-querying always
reflects current state, and the winner's incremented counters naturally move them down the
ladder.

**Why this terminates.** Each lost race means another worker consumed a slot, so the set of
claimable users shrinks; the loop exits when the query returns nothing. A concurrent
*completion* can add a slot back, so the loop is not bounded in theory — but at manual task
creation rates, contention is effectively zero, and a task that falls through to the pool is
retried by the §5 triggers anyway. Self-correcting either way.

### Measured: what the ladder actually costs

PostgreSQL 14, 100k users, 3.19M eligibility rows (327 MB — 102 bytes/row, against the §10
estimate of ~100), widest rule 25k eligible users. **Mean ms per execution over 200 runs after
a 20-run warmup**, which is more representative of steady state than a single
`EXPLAIN ANALYZE` — that adds per-node instrumentation overhead and measures a cold-ish cache.

| Configuration | Widest (d≈0.25) | Narrow (d≈0.04) | Narrowest (d≈0.01) |
|---|---|---|---|
| Default planner, no selection index | 33.6 ms | 4.4 ms | 3.1 ms |
| **+ `users_selection_order`, `random_page_cost=1.1`** | **8.7 ms** | **0.41 ms** | **0.41 ms** |

`LIMIT 1` versus `LIMIT 20`, same configuration: 8.71 vs 8.60 ms, 0.412 vs 0.412 ms, 0.405 vs
0.407 ms. **Removing the candidate limit costs nothing measurable** — Postgres was already
using a bounded top-N heapsort, and the walk plan stops at the first match either way.

**The ladder is not the cost either.** Against a single-key round-robin ordering the four-key
ladder measured 31.9 ms vs 24.7 ms on the unindexed plan — a 7 ms difference on a 32 ms query,
most of it row width. Round-robin is rejected on evidence: it buys ~20% on a query whose cost
lay entirely in the join plan, and forfeits the least-load property to do it.

**Key 4 is not decorative — measured.** On this dataset **17 users tie the winner on keys 1–3**
(same committed hours, same lifetime hours, same account-creation timestamp). Without `id` as
a final key, which of those 17 gets the task is whatever the engine returns that day.

**Round-robin is rejected on evidence, not preference:** it buys ~20% on a query whose cost
lies elsewhere and forfeits the entire least-load property. The remaining 25 ms was the *join
plan*, not the ordering — fixed by the index in §4, which lets Postgres walk the ladder order
and stop at 20 instead of collecting 25,000 rows and sorting them.

**The plan switch is retracted — the planner does it unaided.** An earlier draft stored
`rules.eligible_count` so the application could choose between an ordered-index walk and a
join, on the theory that the planner had no per-rule selectivity statistics. Measured, it
picks the walk by itself:

```
Limit
  -> Nested Loop
       -> Index Scan using users_selection_order on assignment_user u
            Filter: (active_task_count < 5)
       -> Index Only Scan using uniq_rule_user on assignment_ruleeligibleuser reu
            Index Cond: ((rule_id = 42) AND (user_id = u.id))
            Heap Fetches: 0
```

That is the intended shape exactly: walk the ladder order, probe eligibility, stop at the
first match. No crossover was found across `R` from 10² to 5×10³, so the switch would have
been machinery guarding a case that does not arise. `eligible_count` is kept — it is what
distinguishes the structural from the transient case in §6 — but nothing reads it to pick a
plan.

**Caveat on these numbers:** the seed correlates rule membership with load in a way real data
would not, which distorts walk length. Direction and order of magnitude hold; exact figures
need a realistic distribution.

---

## 7. Caching strategy

**Cache the stable half, never the volatile half** — the direct consequence of D2.

| What | Where | Invalidated by | Built? |
|---|---|---|---|
| Rule *spec* (`predicates`, `max_active_tasks`) | Django cache, no expiry | never — these fields are immutable (D4) | ✅ |
| Single-flight lock on materialisation | Django cache, 300 s TTL | released on completion | ✅ |
| Recompute debounce per user | Django cache, short TTL | window expiry | ✅ |
| `active_task_count`, `committed_effort_hours`, `lifetime_hours` | **never cached** | — read fresh per query | — |
| ~~Stable eligible set per rule~~ | ~~Redis set~~ | — | **dropped, see below** |

**Only the immutable half of a rule row is cached.** `eligible_count` and `materialized_at`
sit on the same row and change on every materialisation. Caching the whole model instance
would serve a stale count to the "no eligible users" branch in §6 and misreport *why* a task
is unassigned — the one thing that branch exists to get right. `rule_spec()` returns
predicates and cap only, and a test asserts it carries nothing else.

**Why the volatile columns are never cached (D14):** a cached eligible-user list stale on load
hands out assignments that violate the cap, the one invariant §6 exists to protect.

**Why single-flight is worth building even though latency is fine (Q-4):** it is not a latency
optimisation. Rematerialising a rule is the same work whoever does it, so N workers reacting
to one invalidation would do the scan N times. The loser skips rather than queues. The lock
carries a TTL so a worker dying mid-scan cannot wedge a rule permanently — and a test asserts
the lock is released even when materialisation raises.

**The per-rule eligible-set cache was dropped on evidence.** It appeared in earlier drafts as
a Redis set. Two measurements killed it: the query it would replace runs at p95 1.65 ms at
full scale, and the volatile filter has to hit the database regardless — so the cache would
add an invalidation surface to save nothing. Caching an eligible set of up to 25,000 ids only
to then query `users` for their live load is strictly more work than the join.

## 8. API

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/signup`, `/auth/login`, `/auth/refresh` | JWT + refresh |
| POST | `/tasks/` | create with rules; returns fingerprint and assignment outcome |
| GET | `/tasks/{id}` | task detail + current assignment outcome; what the UI polls |
| PATCH | `/tasks/{id}` | rule change repoints `rule_id` |
| GET | `/tasks/{id}/eligible-users` | cached stable set ∩ live cap filter, ladder order |
| GET | `/my-eligible-tasks` | tasks assigned to the caller |
| POST | `/tasks/recompute-eligibility` | admin escape hatch; enqueues, returns 202 |

**`/my-eligible-tasks` returns the intersection, not the union.** The brief's "eligible **and**
assigned" reads two ways; it is the conjunction. A user sees a task only once it has been
assigned to them, and assignment already implies eligibility:

```sql
SELECT ... FROM tasks
 WHERE assignee_id = %(user)s AND status <> 'done'
 ORDER BY priority ASC, due_date ASC;
```

served by `tasks (assignee_id, priority, due_date) WHERE status <> 'done'`.

**Consequence: there is no self-service pool.** Users cannot browse unassigned work, which is
consistent with the brief's premise that tasks are not manually assigned. Visibility follows
assignment — so priority, which decides assignment order, is what determines who sees a task
and when.

**This endpoint does not touch `rule_eligible_user`.** The reverse index is still needed for
`recompute_user` diffs and the pool query (§5, §6), but this read path is a plain lookup on a
user's own rows.

**Assignment is asynchronous, so the create response cannot know the outcome.** `place_task`
is queued; the worker runs it milliseconds later. The response therefore reports `pending`
rather than guessing — an earlier version guessed, and returned "43 users match, all at
capacity" for a task the worker assigned a second afterwards.

`tasks.placement_attempted_at` is what makes this exact rather than inferred. Without it an
unassigned task is ambiguous between *the worker has not run yet* and *the worker ran and
found nobody* — two states needing opposite responses. `place_task` stamps it whatever the
outcome, so any later read distinguishes them from the row itself:

```
assignee set                          -> assigned
placement_attempted_at IS NULL        -> pending
stamped, eligible_count = 0           -> structural: no user matches this rule
stamped, eligible_count > 0           -> transient: all matching users at capacity
```

The UI polls `GET /tasks/{id}` until the outcome settles, and says so plainly if it never
does rather than inventing a reason — "still unassigned after 6s — is the Celery worker
running?" is a more useful thing to show than a confident wrong diagnosis.

**Why recompute is 202 (D17):** a full recompute at this scale is minutes of worker time. A
synchronous endpoint would either time out or lie about what it costs. It returns a job id, is
idempotent per rule, and is an operational tool for after a bad migration — not a request-path
operation.

---

## 9. Consistency

Stated with windows, because "eventually consistent" without one is not a design.

- **Assignment and the task cap are strongly consistent.** Enforced transactionally by the CAS
  in §6, verified under 16-way concurrency. A cap is never exceeded.
- **Stable eligibility is eventually consistent**, bounded by queue latency — roughly one
  worker hop. A user promoted to 4 years' experience becomes eligible for matching tasks within
  that window.
- **Pool drain is eventually consistent**, same bound, with the backstop sweep as the outer
  limit if a queue message is lost.
- **`/my-eligible-tasks` is strongly consistent** — it reads committed rows with no cache.

**Why the asymmetry (D16):** the two failure modes differ. Stale eligibility costs a delayed
assignment, and the next event corrects it. An exceeded cap is a broken promise to a user who
is now overloaded, and nothing corrects it. Consistency is bought where failure is permanent
and skipped where it is self-healing.

---

## 10. Scale check

### What each cost actually depends on

```
cost                              depends on     magnitude
──────────────────────────────────────────────────────────────────────────
task CRUD                         —              ordinary row ops
assignment claim (CAS)            —              one-row UPDATE
/my-eligible-tasks                —              index lookup, ≤ cap rows
materialize_rule (one rule)       U              one 100k-row scan, worker-side
/tasks/{id}/eligible-users        d              4–12 ms measured (§6)
recompute_user (predicate loop)   R              in-memory, cached rules
recompute_user (row diff)         R · d          rows written per change
rule_eligible_user size           R · d · U      see sensitivity below
```

The first four rows are unaffected by both unknowns. The design's exposure is confined to the
last four.

### Sensitivity of `rule_eligible_user` to R and d

Rows `= R · d · U`, with `U = 10⁵`, at **162 bytes/row measured** on the real Django schema
(2380 MB / 15,384,332 rows). The design's estimate was ~100; see the correction below.

| R \ d | 0.01 | 0.05 | 0.25 |
|---|---|---|---|
| 10² | 10⁵ rows (~16 MB) | 5×10⁵ (~81 MB) | 2.5×10⁶ (~405 MB) |
| 10³ | 10⁶ (~162 MB) | 5×10⁶ (~810 MB) | **2.5×10⁷ (~4 GB)** |
| 10⁴ | 10⁷ (~1.6 GB) | 5×10⁷ (~8 GB) | 2.5×10⁸ (~40 GB) |
| 10⁵ | 10⁸ (~16 GB) | 5×10⁸ (~81 GB) | 2.5×10⁹ (~405 GB) |
| 10⁶ = T | 10⁹ (~162 GB) | 5×10⁹ (~810 GB) | 2.5×10¹⁰ (~4 TB) |

**The row-count model is exact, and was checked rather than trusted.** Seeded at `R = 10³`,
`U = 10⁵`, measured `d = 0.1538`: predicted `10³ × 0.1538 × 10⁵ = 15,380,000`, actual
**15,384,332**. The formula is not an approximation.

**Correction, measured: 162 bytes/row, not ~100.** The estimate came from tuple width for a
bare `(bigint, bigint)` table with two indexes. The real schema carries more:

| Index | Size at 15.4M rows | Why |
|---|---|---|
| `(user_id, rule_id)` | 686 MB | the reverse direction, §4 |
| `uniq_rule_user` | 599 MB | stands in for the composite PK Django 4.2 cannot express |
| implicit `id` PK | 330 MB | the same limitation |
| ~~FK `user_id`~~ | ~~135 MB~~ | **removed** — redundant with `(user_id, rule_id)` |
| ~~FK `rule_id`~~ | ~~98 MB~~ | **removed** — redundant with `uniq_rule_user` |

Django adds a single-column index per ForeignKey by default; both were fully covered by the
composite indexes already present. `db_index=False` on each dropped 233 MB, taking the table
from 2613 MB to 2380 MB. The remaining gap to the design's ~100 bytes/row is the 330 MB
implicit primary key, which Django 5.2's `CompositePrimaryKey` would remove.

The bottom row is the degenerate case: when every task carries a unique rule, `R = T` and
per-rule materialisation *is* per-task materialisation. No leverage there, by construction.

**Derived break-even.** Taking ~10⁹ rows as where a single Postgres instance becomes
operationally unpleasant (vacuum and index maintenance, not raw storage):

```
R · d · U  <  10⁹     with U = 10⁵
    R · d  <  10⁴
```

**Upper bound on d, derived rather than assumed.** A rule naming exactly one of four
departments matches at most `1/4` of users, so `d ≤ 0.25` for such a rule — *provided users are
evenly distributed across departments*, a declared assumption, not a given. Further predicates
only reduce `d`. A rule omitting department entirely is unbounded and may approach `d = 1`.

At the worst bounded case `d = 0.25` the design tolerates `R < 4 × 10⁴`; at `d = 0.05`,
`R < 2 × 10⁵`. Neither is a prediction of R — R remains unmeasured.

### Why being wrong about R is survivable

Correctness never depends on R or d; only latency does. Two structural protections:

1. **Lazy materialisation.** A rule is materialised on first demand, not at creation, and its
   set is evicted once no open task references it. The table is bounded by *actively
   referenced* rules, not by every rule ever authored.
2. **Compiled-`WHERE` fallback.** Any rule can be answered by executing `to_sql` output
   directly against `users` — the same predicate, run on request instead of read from a table.
   Slower, never wrong.

Seed data includes a `R = T` case (P-2) so this path is exercised rather than asserted.

---

## 11. Unknowns, and how each is handled

Nothing here fills a gap silently. Every quantity is **given** (stated in the brief),
**derived** (follows from a given), **declared** (an assumption, marked), or **resolved** (the
user decided).

| Unknown | Stakes | Handling |
|---|---|---|
| `R` — distinct rule cardinality | Medium — was High | **Measured (SP-1): R drives storage, not latency.** Swept `R` from 10² to 5×10³ at fixed `U`; p95 stayed flat on every request-path query (`/my-eligible-tasks` 0.69–1.05 ms across a 50× change in R). No value tested came near the latency budget. The binding constraint on R is disk and vacuum, which is what §10's break-even models. The real R remains unknowable before the system runs; it no longer needs to be known |
| `d` — rule selectivity | Medium — sizes the table, changes no decision | Not assumed. Bounded above at 0.25 for single-department rules (derived); varied across §10's table |
| Request throughput (QPS) | Medium — was High | **Largely resolved.** Percentiles confirmed as p50/p95. Write load is **derived**: tasks are authored manually by Managers, so assignment is human-paced — single-digit writes/sec even at 100k users, against a measured 8.7 ms worst-case candidate query. Read load on `/my-eligible-tasks` scales with users checking their queue, not with task creation, and is a bounded index lookup. P-4 still records the rate it measures at rather than asserting one |
| Crossover `d` between walk and join plans | — | **Resolved (SP-3): there is no crossover to manage.** The planner drives the candidate query from `users_selection_order` unaided across every `R` and `d` tested. The application-level plan switch is retracted (§6) |
| "eligible **and** assigned" in Story 2 | Medium | **Resolved: the conjunction.** Only assigned tasks are visible; no self-service pool. Visibility follows assignment, which is why priority determines access (§8) |
| Preemption | High | **Resolved: none.** A P0 with no eligible user waits like any task, then wins the first freed slot. Priority governs queue position, not eviction (D12) |
| Ordering within a priority band | — | **Given: `created_at ASC`** |
| Department distribution across users | Low | **Declared: even.** Used only to derive the `d ≤ 0.25` bound. Skew raises the bound for large departments |
| Single or multiple assignees per task | Medium | **Declared: single.** The brief says "assigns the task to *the* eligible user". Multi-assignee changes the counter semantics in §6 |
| Does `active_task_count` count `todo` or only `in_progress` | Low | **Declared: both** (any non-`done` task) |
| Source and trust of `effort_hours` | Medium | **Declared: admin estimate at creation, mutable.** Editing it on an assigned task adjusts `committed_effort_hours` in the same transaction. It gates nothing, so an edit can never invalidate an existing assignment — only reorder future selection |
| Final key `id ASC` in the ladder | Low | **Declared addition.** `created_at` is not unique, so without it the order is not total and determinism fails |
| Number of priority bands | Low | **Declared: open-ended `smallint`, 0 = P0.** Nothing depends on the count |
| Aging threshold for stuck tasks | Low | **Declared operational value.** Surfaces to the admin; never auto-deletes or relaxes the rule |
| **What a Manager does** | Medium | **Resolved by the user.** Managers author rules and tasks; the task auto-assigns; the Manager is notified on completion. This added `tasks.created_by` and the `notifications` table (§2) — neither is in the brief. Admin is left as system administration: user management and the recompute escape hatch |
| **Cancellation vs. completion** | Medium | **Declared.** The brief has only `done`. A cancelled task must free the slot and decrement `committed_effort_hours`, but must **not** credit `lifetime_hours` — no work was delivered, and crediting it would corrupt selection key 2. Implemented as a distinct terminal status |
| Manual unassign / reassign | Medium | **Declared: not supported.** The brief's premise is that assignment is not manual. If an admin could pull a task back, that frees capacity and needs a `fill_capacity` trigger — cheap to add, but it is not in the brief and is not built |
| System default for `max_active_tasks` | Low | **Declared: none.** Per-rule and nullable; a rule omitting it has no task cap. The brief's `< 5` is an example inside a rule, not a global setting |
| Storage is SSD | Low | **Declared.** `random_page_cost = 1.1` (§4) is wrong on spinning disk. Revisit if deployed on network storage with different seek characteristics |
| Single PostgreSQL instance | Medium | **Declared.** All of §10 assumes one instance — no sharding, no read replicas. The break-even at `R · d < 10⁴` is a single-node figure |

Two entries remain unresolved, and are named here rather than papered over:

- **`R`** — no seed can answer a question about admin behaviour in a system nobody has run.
  Handled by measuring the design's *tolerance* instead (SP-1).
- **Plan crossover `d`** — needs a measurement not yet taken (SP-3).

Neither blocks the build, and neither is a question anyone can currently answer: the first is
unknowable before the system runs, the second is a measurement rather than a decision.

**Removed from this table** as the design settled: the candidate `LIMIT` (deleted — the ladder
is total, so `LIMIT 1` is exact), the row-size estimate (measured at 102 bytes/row), and the
Manager role and throughput questions (resolved above).

---

## 12. Evidence — what is measured and what is not

Measured at **100k users / 1M tasks / R=1,000 / 15.4M eligibility rows** unless noted.

| Claim | Status | How |
|---|---|---|
| No sequential scan on any request-path query | **Verified at full scale** | `manage.py benchmark` asserts it; P-3 passes |
| p95 ≤ 1.65 ms on both read endpoints, 8 workers | **Verified at full scale** | 4,991–8,778 req/s achieved |
| p95 ≤ 0.81 ms on both assignment queries | **Verified at full scale** | 9,943–14,423 req/s achieved |
| `rows = R · d · U` is exact | **Verified** | predicted 15,380,000, actual 15,384,332 |
| `rule_eligible_user` is 162 bytes/row | **Verified** | 2380 MB / 15.4M rows — see §10, the design estimated ~100 |
| Django's implicit per-FK indexes are redundant | **Verified** | dropping both saved 233 MB at 15.4M rows |
| `R = T` degenerates to per-task materialisation | **Verified** | `seed_scale --degenerate`: 5,000 tasks / 5,000 rules, 1:1 |
| Two assignment paths let P2 overtake P0 | **Verified** | reproduced, then fixed, in the test suite |
| The single primitive fixes it | **Verified** | same test, adversarial interleaving forced |
| Drain order is priority, then age | **Verified** | 33-test suite |
| The cap holds under concurrency | **Verified** | 16 barrier-synchronised workers: 1 won, 15 lost, 0 errors |
| `to_sql` ≡ `matches` | **Verified** | exhaustive property test over a generated rule space |
| Ladder key 4 (`id`) actually fires | **Verified** on the synthetic dataset | 17 users tied the winner on keys 1–3 |
| Four-key ladder costs ~nothing vs. round-robin | **Verified** on the synthetic dataset | 31.9 vs 24.7 ms, both dominated by the join plan |
| ~~`random_page_cost=1.1` is worth 2×~~ | **Retracted** | held on the synthetic schema only; on the real one it moved p95 by 0.02 ms |
| Write load is human-paced | **Derived, not measured** | tasks are authored manually, so assignment rate is bounded by Manager activity |
| `R` drives storage, not latency | **Verified (SP-1)** | swept R 10²→5×10³; p95 flat on every request-path query |
| The planner picks the ordered-index walk unaided | **Verified (SP-3)** | `EXPLAIN` shows `users_selection_order` driving; no plan switch needed |
| `users_selection_order` bloats and `VACUUM` will not reclaim it | **Verified (SP-4)** | 216 kB → 23 MB over 400k updates; `REINDEX` → 112 kB |
| Rule-spec cache and single-flight behave under failure | **Verified** | lock released even when materialisation raises |
| `docker compose up` | **Unverified** | Docker unavailable on the build machine |
| End-to-end HTTP latency | **Unmeasured** | the benchmark is SQL-layer only, stated in its docstring |

**Seed caveat.** `seed_scale` writes the *outcome* of assignment in bulk SQL rather than
driving the assigner, because placing 1M tasks one at a time would take hours. A clean run
of it is not evidence the assigner is correct — the 33-test suite is. Two synthetic-data
artefacts were found and fixed while building it, both of which had silently invalidated a
measurement: rule membership correlated with load, and task status correlated with rule id
(10% of rules held 100% of the open work, so the capacity filter matched nobody). The current
seed reports `users under cap` precisely so that failure cannot recur unnoticed.

## 13. Scope

**Built and verified:** the rule engine with both evaluators, content-addressed
materialisation, delta recompute with debounced signal wiring, the single assignment
primitive with the four-key ladder and the atomic claim, the pool with its structural/transient
discrimination, priority ordering, completion and cancellation bookkeeping, transactional
notifications, the operational sweep and aging flag, rule-spec caching with single-flight,
JWT auth with role enforcement, seeds at both demo and stated scale, an OpenAPI schema, and a
React admin surface.

**Deliberately thin, as declared from the start:** the React UI is a functional admin surface
— sign in, build a rule, watch the task place itself, inspect the ladder, complete work, read
notifications. It is not a design exercise, and it appears in none of the brief's evaluation
criteria. Its rule builder mirrors the closed predicate set exactly, because there is no rule
DSL to expose (D3): the shape of that form *is* the shape of a rule.

**Deliberate simplifications, each with its ceiling named:**

- **JWT in `localStorage`.** Adequate for an internal admin surface, vulnerable to XSS.
  httpOnly cookies with CSRF protection would be the production choice.
- **No rule DSL** (D3), no separate rule service, no event sourcing, no preemption (D12).
- **`RuleEligibleUser` carries an implicit primary key** because Django 4.2 cannot express a
  composite one — 330 MB of the measured 2380 MB at 15.4M rows. Django 5.2 removes it.
- **Notification delivery is polling.** The table is the source of truth; email, websocket or
  push layers on top and is not built.
- **Signals do not see `queryset.update()` or `bulk_create()`.** Django's contract, documented
  in `signals.py`; the seed commands call `recompute_user` themselves.

**Tests cover** the rule engine's two evaluators (exhaustively, against each other), the
priority-overtake race, the capacity race under 16-way concurrency, drain ordering, recompute
deltas, the signal fast path, cache and single-flight failure modes, the operational sweep,
the aging flag, and rule repointing. Not CRUD serialisers — those fail loudly and cheaply.

**What a reviewer should check first:** `docker compose up`, since it is the one path that was
never executed. Then §12, which lists exactly what was measured and what was reasoned.
