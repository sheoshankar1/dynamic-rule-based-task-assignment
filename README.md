# Dynamic Rule-Based Task Assignment

A task management system where tasks are never assigned by hand. Each task carries a rule
describing who may do it; the system computes eligible users and assigns the task in the
background, in priority order.

**Django + DRF · PostgreSQL · Redis · Celery · React · Docker Compose**

---

## Full documentation

**→ [EXPLAINER.md](EXPLAINER.md)** is the single reference document. It covers every
requirement, the solution built for it, and why that solution is necessary — with the
measurements and the architecture diagrams.

The topics the brief asks a README to explain, and where they are:

| Topic | Section |
|---|---|
| Architecture decisions | [§1 Assignment engine](EXPLAINER.md#1-assignment-engine), [§2 Selection](EXPLAINER.md#2-selection) |
| Rule engine design | [§1.1–§1.6](EXPLAINER.md#11-eligibility-storage) |
| Recompute strategy | [§4 Recompute](EXPLAINER.md#4-recompute) |
| Indexing strategy | [§7.1 Indexing](EXPLAINER.md#71-indexing) |
| Caching strategy | [§7.2 Caching](EXPLAINER.md#72-caching) |
| Background processing | [§6](EXPLAINER.md#6-background-processing) |
| Concurrency safety | [§3](EXPLAINER.md#3-concurrency) |
| Data model | [§9](EXPLAINER.md#9-data-model) |
| Assumptions and open questions | [§13](EXPLAINER.md#13-assumptions-and-open-questions) |
| Requirement traceability | [§14](EXPLAINER.md#14-requirement-traceability) |
| Measured vs. reasoned | [§15](EXPLAINER.md#15-evidence-measured-versus-reasoned) |

---

## Running it

```bash
docker compose up --build
```

| | |
|---|---|
| UI | http://localhost:5173 |
| API docs (Swagger) | http://localhost:8000/docs |
| OpenAPI schema | http://localhost:8000/schema |

Logins after seeding: `manager` / `manager`, `admin` / `admin`, or any `userNNNN` / `demo`.

Without Docker, and the test suite: [EXPLAINER §A](EXPLAINER.md#a-running-it).

---

## In one paragraph

Eligibility is stored per **rule**, not per task — rules are canonicalised, hashed and
deduplicated, so a million tasks collapse onto a few hundred distinct rules and each eligible
set is computed once. Rule predicates are split by volatility: department, experience and
location are materialised, while the capacity count is applied as a live filter, because it
changes on every single assignment and materialising it would mean permanent recomputation.
Assignment has exactly one code path, reached by five events, so a waiting P0 can never be
overtaken by a newly created task. Capacity is claimed with a compare-and-set, so two workers
cannot both pass the same check.

Measured at 100,000 users and 1,000,000 tasks: p95 under 1.7 ms on every request-path query,
no sequential scans.

---

> This is an interview exercise. Every credential in the repository is a deliberate
> placeholder — `SECRET_KEY` defaults to `dev-only-not-for-production`, the Postgres password
> never leaves the compose network, and all are environment-overridable.
> [EXPLAINER §13](EXPLAINER.md#13-assumptions-and-open-questions) lists the security
> simplifications and what production would use instead.
