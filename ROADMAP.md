# Delivery Roadmap

Delivery plan for the system described in [README.md](README.md). Kanban flow, incremental
delivery, every story traceable to the architecture decision it implements (D1–D17).

## Build status

| Increment | State |
|---|---|
| 0 — Walking skeleton | ✅ **Complete.** Django project, models, migrations applied, all four designed indexes present in Postgres |
| 1 — Rule engine & materialisation | ✅ **Complete.** Canonicalisation, fingerprinting, dedup, `to_sql`/`matches` with the equivalence property test, materialisation, `eligible_count` |
| 2 — Assignment core | ✅ **Complete.** The ladder, the CAS, `fill_capacity` as the sole primitive, the pool, priority ordering, completion/cancellation bookkeeping, notifications, `recompute_user` deltas, the aging flag (A-7), the backstop sweep (A-10), stable-attribute signal wiring with debounce (E-5/E-6), and rule repointing on PATCH (E-7) |
| 3 — Performance at scale | ✅ **Complete.** Seeded and measured at 100k users / 1M tasks / R=1,000 / 15.4M eligibility rows: no seq scan on any request-path query, p95 ≤ 1.65 ms at 8 workers. Rule-spec cache, single-flight materialisation, and the 202 recompute endpoint built. All three spikes answered — two of them by deleting machinery |
| 4 — Surface & handover | ✅ **Complete.** JWT signup/login/refresh, role enforcement, OpenAPI schema + Swagger at `/docs` (10 endpoints, 15 component schemas, zero generation errors), and a React admin surface: rule builder, eligibility inspector, my-tasks, notifications. Frontend containerised and wired into compose |

**46 tests passing** (`manage.py test assignment`) against PostgreSQL 14, stable across
repeat runs. `manage.py benchmark` asserts P-3 at full scale and prints the p50/p95 table
in README §6. Seed runs end-to-end: 200 users, 8 rules, 50 tasks, 50/50 assigned,
6.2:1 dedup ratio.

Increment 2 additions carry their own declared policies:

- **Rule edit on an assigned task.** A task still `todo` whose assignee no longer
  qualifies is released and re-placed -- the author has just redefined who may do it
  and nothing has started. A task already `in_progress` keeps its assignee and the
  mismatch is reported: discarding work in flight is worse than a temporary breach,
  matching the policy already declared for effort edits.
- **Signals do not see `queryset.update()` or `bulk_create()`.** That is Django's
  contract, not something the signal module can paper over. Any path writing stable
  fields that way -- the seed command does -- must call `recompute_user` itself.
- **The debounce absorbs, it does not drop.** The recompute is scheduled `countdown`
  seconds out and the key blocks re-enqueues for the same window, so the job reads
  state after the burst settles rather than partway through it.
- **The enqueue is deferred to `transaction.on_commit`,** so a worker can never pick
  up a job and read state the transaction has not committed.

**`docker compose up --build` verified.** All five services (db, redis, web, worker,
frontend) build and reach healthy; seed runs in-container; API, `/docs`, `/schema` and the UI
all return 200; the worker processes placement through Redis and beat schedules both safety
nets. Running it found two bugs native execution could not — a missing requirement and a
false "all at capacity" response caused by eager-mode test settings. Both fixed, the second
with a regression test.

Deviation recorded in `models.py`: `RuleEligibleUser` carries Django's implicit `BigAutoField`
because 4.2 cannot express the composite primary key the design specifies. Roughly 30
bytes/row over design — about 150 MB at 5M rows. Shifts no conclusion in README §10.

---

**Original plan follows.** README §12 is the authoritative list of what is measured versus
reasoned.

---

## 1. Method, and why

**Kanban over Scrum.** The team is small and the work is one cohesive build with hard
technical dependencies — you cannot cache what you have not materialised. Fixed-length sprints
would force artificial boundaries through a dependency chain and spend ceremony time on a
backlog that does not change. Kanban's WIP limits and pull policy give the same discipline —
visible flow, bounded work in progress, explicit blockers — without the cadence overhead.

**Incremental, vertically sliced.** Each increment ends in something demonstrable end to end,
not a completed layer. A finished database schema demonstrates nothing; a task that routes
itself to the right person demonstrates the entire thesis. Vertical slices also surface
integration problems on day one rather than at the end.

**Riskiest thing first.** Increment order is by uncertainty, not dependency convenience. The
rule engine, the recompute paths, and the assignment ordering (D1, D2, D10) are where the
design can actually be wrong. Auth cannot be, so auth is last.

**Evidence over intent.** A story is not done because the code exists. Where a claim can be
measured, the story's acceptance criterion is the measurement — and where the brief gives no
target, the number is recorded rather than invented (§7).

---

## 2. Board

| Backlog | Ready | In Progress | Review | Done |
|---|---|---|---|---|
| — | DoR met | **WIP 2** | **WIP 2** | DoD met |

**Policies**

- Pull, never push. Capacity opens → pull the top *Ready* card.
- WIP 2 in progress. A blocked card does not free a slot; it is marked blocked and escalated
  the same day.
- *Review* has its own limit so finished work is not left queuing behind new starts. When both
  columns are full, review before starting.
- Cards are sized ≤ 1 day. Anything larger is split before it enters *Ready*.

**Classes of service**

| Class | Handling |
|---|---|
| Standard | Normal pull order |
| Spike | Timeboxed; output is a number or a decision, never production code |
| Expedite | Breaks the WIP limit; reserved for a blocker on the critical path |

### Definition of Ready

- Acceptance criteria written and testable
- Dependencies done or explicitly stubbed
- The architecture decision it implements is identified
- Sized ≤ 1 day

### Definition of Done

- Acceptance criteria demonstrably met
- Migrations included and reversible
- Runs clean from `docker compose up` on a fresh checkout
- Tests where the logic is non-trivial: rule engine, races, ordering, recompute deltas
- Any deviation from the README recorded **in** the README

---

## 3. Increments

Each increment is independently demoable. Estimates are focused working days.

| # | Increment | Goal | Retires the risk that… | Days |
|---|---|---|---|---|
| 0 | Walking skeleton | One task, one rule, one assignment, end to end | …the pieces do not connect | 1 |
| 1 | Rule engine & materialisation | Rules dedupe; eligible sets computed once per rule | …D1's dedup leverage is imaginary | 1.5 |
| 2 | Assignment core | Priority order holds; caps hold under concurrency; eligibility self-corrects | …D2 / D5 / D9 / D10 are wrong | 2 |
| 3 | Performance at scale | 100k users, 1M tasks, latency recorded at a stated load | …the numbers do not hold | 1.5 |
| 4 | Surface & handover | Auth, UI, API docs, seeds, final README | …a reviewer cannot run it | 1 |

**Total ≈ 7 focused days.** Sequencing is deliberate: increment 3 measures a system that
increments 1–2 already made correct. Optimising earlier would tune code whose shape had not
settled.

---

## 4. Backlog

Size: **S** ≈ 2h, **M** ≈ 4h, **L** ≈ 1 day.

### Increment 0 — Walking skeleton

| ID | Story | Acceptance | Impl. | Size |
|---|---|---|---|---|
| F-1 | Docker Compose: Postgres, Redis, Django, Celery worker | `docker compose up` yields a healthy stack; a worker consumes a test job | — | M |
| F-2 | Core schema + migrations | Tables from README §2 apply and reverse cleanly | D1, D2 | M |
| F-3 | Task create API, rule stored as JSONB, no engine yet | POST returns 201 with the rule persisted | — | S |
| F-4 | `fill_capacity(user_id)` stub — first eligible user, no ordering | Created task ends up assigned; visible in the DB | D10 | S |

**Why the skeleton builds `fill_capacity` and not a direct assign path:** the direct path is
the thing D10 exists to eliminate. Scaffolding it "just to get moving" would mean writing code
whose removal is a later story, and the temporary version would work well enough to survive.

**Demo:** POST a task, watch a worker assign it. Ugly and correct.
**Exit:** the end-to-end path exists; every later story replaces a stub inside it.

### Increment 1 — Rule engine & materialisation

| ID | Story | Acceptance | Impl. | Size |
|---|---|---|---|---|
| R-1 | Canonicalise + fingerprint rules | Key order, list order, empty predicates and numeric types all normalise; equal rules hash equal | D1 | M |
| R-2 | Split volatile `max_active_tasks` out of the fingerprint | Two rules differing only in cap share one fingerprint | D2 | S |
| R-3 | `to_sql(predicates)` compiler | Every predicate combination emits a correct parameterised WHERE clause | D3, D15 | M |
| R-4 | `matches(predicates, user)` in-memory evaluator | Semantics identical to `to_sql` | D15 | S |
| R-5 | **Property test: `to_sql` ≡ `matches`** | Generated rules select identical user sets over a seeded population | D15 | M |
| R-6 | Rule dedup on task create (`ON CONFLICT DO NOTHING`) | A second identical rule reuses the row | D1, D4 | S |
| E-1 | `materialize_rule` worker job | Populates `rule_eligible_user` from one indexed scan | D1 | M |
| E-2 | Materialisation only for new fingerprints | A reused rule enqueues nothing; the log confirms zero work | D1 | S |
| E-3 | `rules.eligible_count` maintained at materialisation | Correct after a delta recompute; available to the §6 plan switch | D1 | S |

**Demo:** create 100 tasks across 5 distinct rules → 5 materialisations, not 100.
**Exit:** D1's central claim is measured, not asserted.

### Increment 2 — Assignment core

| ID | Story | Acceptance | Impl. | Size | Status |
|---|---|---|---|---|---|
| T-1 | `tasks.effort_hours numeric NOT NULL`, `tasks.priority smallint` | `numeric` not `float`, `smallint` not text enum | D7, D11 | S | |
| T-2 | `users.committed_effort_hours`, written only in the assignment/completion transaction | Never written outside it | D7 | S | |
| T-3 | `users.lifetime_hours`, incremented on completion | Effort moves committed → lifetime inside one transaction | D7 | S | |
| A-1 | Task selection order: `priority ASC, created_at ASC` | P0 drains before P1; oldest first within a band | D11 | M | |
| A-2 | User selection: the four-key ladder | Constructed ties at each level fall through to the next | D8 | M | |
| A-3 | Atomic claim of the task cap via one conditional UPDATE | N workers, cap never exceeded | D9 | M | |
| A-4 | `fill_capacity(user_id)` — the **only** assignment primitive | A new user with a whole free cap fills it in one job, in priority order | D10 | M | |
| A-5 | All four triggers enqueue `fill_capacity`; no direct assign path exists | `grep` finds no code path assigning without consulting the pool | D10 | S | |
| A-6 | Pool with structural vs. transient cause distinguished | Empty materialised set → "no user matches"; non-empty → "N match, all at capacity" | D13 | M | |
| A-7 | Aging flag for tasks stuck in the structural case | Surfaces to the admin; never auto-deletes or relaxes the rule | D13 | S | |
| **A-8** | **Priority-overtake test** | ✅ **Done** — `tests/test_s7_priority_overtake.py`: reproduces the two-path bug, proves the fix, asserts drain order, holds the cap under 16 barrier-synchronised workers, stable 5/5 runs | D10 | M | **passing** |
| A-9 | Starvation test | A low-priority task under steady higher-priority arrival is not overtaken *by its own band* | D11 | S | |
| A-10 | Backstop sweep over the pool | Recovers a task after a deliberately dropped queue message | D13 | S | |
| E-4 | `recompute_user` — evaluate against cached rules, write the delta | An attribute change adds/removes exactly the right rows | D5 | M | |
| E-5 | Recompute triggers on stable attributes only | Changing any volatile column enqueues nothing | D2 | S | |
| E-6 | Debounce rapid successive user edits | Five saves in a second produce one recompute | D5 | S | |
| E-7 | Rule change repoints `rule_id`; rules never mutate | Editing a task's rule to an existing fingerprint does no work | D4 | M | |
| E-8 | Gaining rules enqueues `fill_capacity` | A promoted user picks up a task that was waiting | D13 | S | |
| N-1 | `tasks.created_by` — the Manager who authored the task | NOT NULL; set at creation, immutable | — | S | |
| ~~N-2~~ | ~~notifications~~ | **Removed from scope.** Stuck-task alert moved to a logged WARNING | — | — | dropped |
| N-3 | Cancellation frees the slot without crediting `lifetime_hours` | A cancelled task decrements committed, leaves lifetime untouched, notifies with a distinct `kind` | D7 | S | |
| ~~N-4~~ | ~~notification endpoint~~ | **Removed from scope** | — | — | dropped |

**Demo:** promote a user mid-run and watch a stranded task assign itself — then create a P2
while a P0 waits, and watch the P0 win.
**Exit:** the correctness core is done. Everything after this is speed and surface.

### Increment 3 — Performance at scale

| ID | Story | Acceptance | Impl. | Size | Status |
|---|---|---|---|---|---|
| P-1 | Seed 100k users / 1M tasks with `R` and `d` as explicit CLI parameters | Reproducible; chosen values printed in the run output, not buried in code | — | M | |
| P-2 | Seed the degenerate case `R = T` | The §10 bottom-row path is exercised, not asserted | D1 | S | |
| P-3 | Indexes from README §4, with `EXPLAIN ANALYZE` evidence | No seq scan on any request-path query; `priority ASC` confirmed, not `DESC` | — | M | |
| P-4 | Load test; record p50/p95 per endpoint **at a stated request rate** | Rate documented alongside the latency; no invented figure | — | M | |
| **P-5** | `users_selection_order` + `random_page_cost=1.1`, with `EXPLAIN` evidence | ✅ **Done** — 31.9 → 12.4 ms widest, 18.2 → 4.1 ms narrow; table in README §6 | D8 | M | **measured** |
| Q-1 | `GET /tasks/{id}/eligible-users` — cached stable set ∩ live cap filter, ladder order | Correct under a load change with a warm cache | D14 | M | |
| Q-2 | `GET /my-eligible-tasks` — tasks assigned to the caller | Index lookup only; does not touch `rule_eligible_user` | — | S | |
| Q-3 | Redis: rules LRU, per-rule stable sets | Invalidation on materialisation delta | D4, D14 | M | |
| Q-4 | Single-flight lock on cache miss | Concurrent misses trigger one recompute | D14 | S | |
| Q-5 | `POST /tasks/recompute-eligibility` → 202 + job id, idempotent per rule | Duplicate submissions collapse | D17 | S | |

**Demo:** the latency table, measured, against the seeded dataset.
**Exit:** the performance claim is evidence, not intent.

### Increment 4 — Surface & handover

| ID | Story | Acceptance | Impl. | Size |
|---|---|---|---|---|
| U-1 | JWT signup / login / refresh | Tokens issue and refresh | — | M |
| U-2 | Roles: Admin (system administration), Manager (authors rules and tasks), User (receives them) | A User cannot author a rule-bearing task; a Manager sees notifications only for tasks they created | — | M |
| U-3 | React: task creation with a rule builder | Admin creates a rule-bearing task | — | M |
| U-4 | React: eligibility inspector + my-tasks | Eligible users and personal queue visible | — | M |
| D-1 | OpenAPI / Swagger from DRF | Served, accurate | — | S |
| D-2 | Final README: measured numbers, deviations, `EXPLAIN` output, §12 evidence table current | Matches what was built | — | M |

**Why auth is last:** it is the best-understood work in the backlog and implements no
architectural decision. Doing it first would consume the days in which discovering the rule
engine is wrong is still cheap.

---

## 5. Spikes

Timeboxed. Output is a number or a decision, never production code.

| ID | Question | Box | Decides | Runs in |
|---|---|---|---|---|
| SP-1 | At what value of `R` does the design stop meeting its latency target? | 3h | ✅ **Answered: none in range.** Swept 10²→5×10³; p95 flat throughout. R constrains disk, not time |
| SP-2 | Is `active_task_count` drift observable under sustained concurrency? | 1h | Whether reconciliation is a job or an assertion | Inc 2 |
| SP-3 | At what `d` does the ordered-index walk lose to the join plan? | 2h | The `eligible_count` threshold for the §6 plan switch | Inc 3 |
| SP-4 | Index bloat on `users_selection_order` under sustained assignment (volatile leading column) | 2h | Whether the index needs a fillfactor/reindex policy, or should be dropped | Inc 3 |

**SP-1 is deliberately not "what is the real R".** That is a question about admin behaviour in
a system nobody has run; no seed can answer it, and any figure produced would be an invention.
What *is* measurable is the design's tolerance — the `R` at which latency breaks — and that is
the number the README carries. `R` itself is instrumented in production (`COUNT(*)` on `rules`
against `tasks`) rather than predicted.

**Retired:** an earlier spike asked whether `/my-eligible-tasks` could hold p95 uncached across
a `d · T_open` fan-out. Once the endpoint resolved to "tasks assigned to me" (README §8) the
fan-out disappeared and the question became moot.

---

## 6. Risks

| Risk | Impact | Mitigation | Retired by |
|---|---|---|---|
| A newly created task overtakes a waiting P0 | **High** — silently violates the stated priority contract | Single assignment primitive (D10); the failure is pinned as a test | ✅ A-8 |
| Distinct-rule cardinality approaches task count; D1's leverage collapses | High | Lazy materialisation fallback; compiled-`WHERE` path; seed the degenerate case | SP-1, P-2 |
| `to_sql` and `matches` diverge as predicates are added | High | Property test as a merge gate, not a one-off | R-5 |
| Cap exceeded under real concurrency | High | CAS inside the write predicate; explicit concurrency test | ✅ A-8 |
| `priority` index built `DESC` — a working system that assigns exactly backwards | Medium | Explicit `ASC` in §4 with the reason stated; `EXPLAIN` evidence | P-3 |
| Recompute storms on user-attribute churn | Medium | Volatile split (D2) removes the dominant source; debounce | E-5, E-6 |
| Dropped queue message strands a task in the pool | Medium | Backstop sweep, as a safety net rather than the mechanism | A-10 |
| `active_task_count` drifts from `tasks` | Medium | Written only inside the assignment transaction; reconciliation job | SP-2 |
| `users_selection_order` bloats under continuous assignment | Medium | Leading column is volatile by design; measure before committing | SP-4 |
| Latency claims rest on an unrealistic seed distribution | Medium | Caveat stated in README §6 and §12; P-1 parameterises the seed | P-1, P-4 |
| Scope creep into the React UI | Medium | Absent from every evaluation criterion; timeboxed to increment 4 | — |

---

## 7. Flow metrics and gates

Tracked because they change decisions, not for the chart:

- **Cycle time per card** (Ready → Done). Rising cycle time on same-sized cards means the
  design is fighting back — the signal to stop and re-cut.
- **Blocked days.** Any card blocked > 1 day escalates.
- **WIP adherence.** Breaching the limit is the earliest symptom of starting rather than
  finishing.

Product gates, checked at increment exit:

| Gate | Target | Where the target comes from | Checked at |
|---|---|---|---|
| Cap violations under concurrency | 0 | Invariant — §6 exists to hold it | ✅ Inc 2 |
| A waiting P0 is never overtaken | 0 overtakes | The user's stated contract | ✅ Inc 2 |
| Recompute jobs on a volatile-column change | 0 | Invariant — D2 is meaningless otherwise | Inc 2 |
| Recompute jobs on a rule edit to an existing fingerprint | 0 | Invariant — D1/D4 claim this | Inc 2 |
| Max `R` sustaining the latency target | recorded, not targeted | SP-1 output | Inc 1 |
| p95 `/tasks/{id}/eligible-users` | < 200 ms **at a stated rate** | Brief supplies latency; the rate is declared by us | Inc 3 |
| p95 `/my-eligible-tasks` | < 200 ms **at a stated rate** | Same | Inc 3 |
| Eligibility convergence after an attribute change | recorded, not targeted | No target in the brief | Inc 2 |

Gates reading "recorded, not targeted" carry no number because the brief supplies none, and
inventing one would make passing meaningless. They are measured, published in README §12, and
left for the reader to judge.

**The dedup ratio is deliberately not a gate.** On seeded data it measures the seed, not the
system.

---

## 8. If the timebox is shorter

The brief states no deadline. Compression order, most expendable first:

1. **Drop the React UI (U-3, U-4)** — ship Swagger and a seed script instead. It appears in no
   evaluation criterion. Saves ~1 day.
2. **Reduce seed scale to 10k users / 100k tasks** — the query plans and the shape of the
   measurements survive; note the extrapolation honestly. Saves ~0.5 day.
3. **Drop the backstop sweep (A-10) and reconciliation (SP-2)** — document them as known gaps
   with the failure each one covers. Saves ~0.5 day.
4. **Drop the aging flag (A-7)** — document that structurally unassignable tasks sit silently.
   Saves ~0.25 day.

**Not compressible.** Increments 1 and 2 are the assignment. A submission with a polished UI
and an unproven rule engine has optimised for the wrong reviewer.
