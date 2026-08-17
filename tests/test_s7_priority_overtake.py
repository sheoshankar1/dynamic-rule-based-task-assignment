"""S-7: a waiting P0 task must be assigned before a newly created P2 task.

Proves two things against a real PostgreSQL instance:

  1. THE BUG   - the two-path design (direct assign on creation + pool drain on
                 completion) lets a newly created P2 overtake a waiting P0.
  2. THE FIX   - routing every trigger through one primitive, fill_capacity(user),
                 makes P0 win even under the worst-case job interleaving.

The race is not left to thread timing. The test forces the adversarial ordering
(the new task's job runs BEFORE the freed-capacity job), because that is the
interleaving the bug needs. A test that hoped to hit it by luck would be flaky
and would pass for the wrong reason.

Flow of the scenario, identical for both designs:

    user U, cap 1, currently at cap (holding T_old)
    P0 task waiting in the pool, eligible for U
      |
      +-- t0  T_old completes        -> slot freed, fill_capacity(U) enqueued
      +-- t1  P2 task created        -> its job enqueued
      +-- t2  P2's job runs FIRST    <-- adversarial interleaving
      +-- t3  fill_capacity(U) runs
      |
      v
    who holds the slot?   old design: P2 (bug)    new design: P0 (correct)

Run:
    pg_ctl -D pgdata -o "-p 5439 -k /tmp" start
    python3 test_s7_priority_overtake.py
"""

import os
import sys
import threading

import psycopg2

DSN = os.environ.get(
    "S7_DSN", "host=/tmp port=5439 user=bench dbname=postgres"
)

SCHEMA = """
DROP TABLE IF EXISTS tasks, rule_eligible_user, rules, users CASCADE;

CREATE TABLE users (
    id                     int PRIMARY KEY,
    active_task_count      int          NOT NULL DEFAULT 0,
    committed_effort_hours numeric(7,2) NOT NULL DEFAULT 0,
    lifetime_hours         numeric(9,2) NOT NULL DEFAULT 0,
    created_at             timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE rules (
    id               int PRIMARY KEY,
    max_active_tasks int
);

CREATE TABLE rule_eligible_user (
    rule_id int NOT NULL,
    user_id int NOT NULL,
    PRIMARY KEY (rule_id, user_id)
);

CREATE TABLE tasks (
    id           int PRIMARY KEY,
    priority     smallint     NOT NULL,      -- 0 = P0
    effort_hours numeric(6,2) NOT NULL,
    status       text         NOT NULL,      -- todo | in_progress | done
    rule_id      int          NOT NULL REFERENCES rules(id),
    assignee_id  int          NULL REFERENCES users(id),
    created_at   timestamptz  NOT NULL
);
CREATE INDEX ON tasks (rule_id) WHERE assignee_id IS NULL AND status <> 'done';
"""


# --------------------------------------------------------------------------
# shared: the atomic claim (README section 6)
# --------------------------------------------------------------------------

def claim(cur, user_id, cap, effort):
    """Reserve one slot on user_id. Returns True if this caller won it.

    The cap is evaluated INSIDE the write, so two concurrent callers cannot
    both pass a read-then-write check.
    """
    cur.execute(
        """
        UPDATE users
           SET active_task_count      = active_task_count + 1,
               committed_effort_hours = committed_effort_hours + %s
         WHERE id = %s
           AND (%s::int IS NULL OR active_task_count < %s)
        RETURNING id
        """,
        (effort, user_id, cap, cap),
    )
    return cur.fetchone() is not None


def candidates(cur, rule_id, cap, limit=20):
    """Eligible users for a rule with a free slot, best first (the ladder)."""
    cur.execute(
        """
        SELECT u.id
          FROM rule_eligible_user reu
          JOIN users u ON u.id = reu.user_id
         WHERE reu.rule_id = %s
           AND (%s::int IS NULL OR u.active_task_count < %s)
         ORDER BY u.committed_effort_hours ASC,
                  u.lifetime_hours        DESC,
                  u.created_at            ASC,
                  u.id                    ASC
         LIMIT %s
        """,
        (rule_id, cap, cap, limit),
    )
    return [row[0] for row in cur.fetchall()]


# --------------------------------------------------------------------------
# OLD design: two paths
# --------------------------------------------------------------------------

def assign_direct(cur, task_id):
    """The removed path: assign THIS task, without consulting the pool.

    This is the bug. It cannot see that a higher-priority task is already
    waiting for the slot it is about to take.
    """
    cur.execute(
        "SELECT rule_id, effort_hours FROM tasks WHERE id = %s", (task_id,)
    )
    rule_id, effort = cur.fetchone()
    cur.execute("SELECT max_active_tasks FROM rules WHERE id = %s", (rule_id,))
    (cap,) = cur.fetchone()

    for user_id in candidates(cur, rule_id, cap):
        if claim(cur, user_id, cap, effort):
            cur.execute(
                "UPDATE tasks SET assignee_id = %s WHERE id = %s",
                (user_id, task_id),
            )
            return user_id
    return None


# --------------------------------------------------------------------------
# NEW design: one primitive
# --------------------------------------------------------------------------

def fill_capacity(cur, user_id):
    """The only assignment primitive.

    Drains the pool of tasks this user is eligible for, in priority order,
    until the user's cap is reached. Because every trigger routes through
    here, a waiting P0 is always considered before a newly created P2.
    """
    assigned = []
    while True:
        cur.execute(
            """
            SELECT t.id, t.effort_hours, r.max_active_tasks
              FROM tasks t
              JOIN rule_eligible_user reu
                ON reu.rule_id = t.rule_id AND reu.user_id = %s
              JOIN rules r ON r.id = t.rule_id
             WHERE t.assignee_id IS NULL
               AND t.status <> 'done'
             ORDER BY t.priority ASC, t.created_at ASC
             LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return assigned

        task_id, effort, cap = row
        if not claim(cur, user_id, cap, effort):
            return assigned  # user is at capacity

        cur.execute(
            "UPDATE tasks SET assignee_id = %s WHERE id = %s",
            (user_id, task_id),
        )
        assigned.append(task_id)


def complete_task(cur, task_id):
    """Completion moves effort from current load to track record, frees a slot."""
    cur.execute(
        """
        UPDATE tasks SET status = 'done'
         WHERE id = %s AND status <> 'done'
        RETURNING assignee_id, effort_hours
        """,
        (task_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    assignee, effort = row
    cur.execute(
        """
        UPDATE users
           SET active_task_count      = active_task_count - 1,
               committed_effort_hours = committed_effort_hours - %s,
               lifetime_hours         = lifetime_hours + %s
         WHERE id = %s
        """,
        (effort, effort, assignee),
    )
    return assignee


# --------------------------------------------------------------------------
# scenario
# --------------------------------------------------------------------------

P0_TASK, P2_TASK, OLD_TASK = 100, 200, 1


def build_scenario(cur):
    """User at cap, a P0 already waiting in the pool."""
    cur.execute(SCHEMA)
    cur.execute("INSERT INTO rules VALUES (1, 1)")          # cap = 1 task
    cur.execute("INSERT INTO users (id) VALUES (1)")
    cur.execute("INSERT INTO rule_eligible_user VALUES (1, 1)")

    # the task currently occupying the only slot
    cur.execute(
        "INSERT INTO tasks VALUES (%s, 2, 3, 'in_progress', 1, 1, now())",
        (OLD_TASK,),
    )
    cur.execute("UPDATE users SET active_task_count = 1, "
                "committed_effort_hours = 3 WHERE id = 1")

    # a P0 that has been waiting because the user was at cap
    cur.execute(
        "INSERT INTO tasks VALUES (%s, 0, 4, 'todo', 1, NULL, "
        "now() - interval '1 hour')",
        (P0_TASK,),
    )


def holder(cur, task_id):
    cur.execute("SELECT assignee_id FROM tasks WHERE id = %s", (task_id,))
    return cur.fetchone()[0]


def run_old_design(cur):
    """Two paths. The new task's job runs first and takes the freed slot."""
    build_scenario(cur)
    complete_task(cur, OLD_TASK)                   # t0: slot freed
    cur.execute(                                   # t1: P2 created
        "INSERT INTO tasks VALUES (%s, 2, 2, 'todo', 1, NULL, now())",
        (P2_TASK,),
    )
    assign_direct(cur, P2_TASK)                    # t2: adversarial ordering
    fill_capacity(cur, 1)                          # t3: too late
    return holder(cur, P0_TASK), holder(cur, P2_TASK)


def run_new_design(cur):
    """One primitive. Creation enters the pool; the drain decides."""
    build_scenario(cur)
    complete_task(cur, OLD_TASK)                   # t0: slot freed
    cur.execute(                                   # t1: P2 created -> pool
        "INSERT INTO tasks VALUES (%s, 2, 2, 'todo', 1, NULL, now())",
        (P2_TASK,),
    )
    fill_capacity(cur, 1)                          # t2: creation's own trigger
    fill_capacity(cur, 1)                          # t3: completion's trigger
    return holder(cur, P0_TASK), holder(cur, P2_TASK)


def run_concurrency_check(conn_factory, threads=16):
    """N workers race to fill one slot. The cap must hold and P0 must win.

    A Barrier holds every worker until all connections are open and every
    thread is at the starting line, so they enter fill_capacity together.
    Without it the threads can serialise by accident and the test passes
    while proving nothing.

    Each worker reports what it observed, so the output shows that
    contention actually happened rather than asserting it did.
    """
    with conn_factory() as setup, setup.cursor() as cur:
        build_scenario(cur)
        complete_task(cur, OLD_TASK)
        cur.execute(
            "INSERT INTO tasks VALUES (%s, 2, 2, 'todo', 1, NULL, now())",
            (P2_TASK,),
        )
        setup.commit()

    errors = []
    outcomes = []                       # 'won' | 'lost-race' | 'no-task'
    gate = threading.Barrier(threads)

    def worker():
        try:
            conn = conn_factory()
            with conn, conn.cursor() as cur:
                cur.execute("SELECT 1")       # force the connection open
                gate.wait()                   # all workers start together
                got = fill_capacity(cur, 1)
                conn.commit()
                outcomes.append("won" if got else "lost-race")
        except Exception as exc:              # surface, never swallow
            errors.append(exc)
            outcomes.append("error")

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    with conn_factory() as check, check.cursor() as cur:
        cur.execute("SELECT active_task_count FROM users WHERE id = 1")
        (count,) = cur.fetchone()
        return count, holder(cur, P0_TASK), holder(cur, P2_TASK), errors, outcomes


def run_priority_ordering_check(cur):
    """Cap 3, four waiting tasks. Assert the drain order, not just the winner.

    Pool (creation order deliberately not priority order):
        id 300  P2  created 09:00
        id 301  P0  created 09:05
        id 302  P1  created 09:10
        id 303  P2  created 08:55   <- oldest P2, must beat 300
    Expected fill order: 301 (P0), 302 (P1), 303 (older P2). 300 waits.
    """
    cur.execute(SCHEMA)
    cur.execute("INSERT INTO rules VALUES (1, 3)")          # cap = 3 tasks
    cur.execute("INSERT INTO users (id) VALUES (1)")
    cur.execute("INSERT INTO rule_eligible_user VALUES (1, 1)")
    for tid, prio, minutes in (
        (300, 2, 0),
        (301, 0, 5),
        (302, 1, 10),
        (303, 2, -5),
    ):
        cur.execute(
            "INSERT INTO tasks VALUES (%s, %s, 1, 'todo', 1, NULL, "
            "timestamptz '2026-01-01 09:00' + %s * interval '1 minute')",
            (tid, prio, minutes),
        )
    return fill_capacity(cur, 1)


def main():
    def connect():
        return psycopg2.connect(DSN)

    failures = []

    with connect() as conn, conn.cursor() as cur:
        p0, p2 = run_old_design(cur)
        print("OLD design (two paths)")
        print(f"  P0 assignee = {p0!r}   P2 assignee = {p2!r}")
        if p2 == 1 and p0 is None:
            print("  -> BUG REPRODUCED: P2 overtook the waiting P0\n")
        else:
            print("  -> bug did NOT reproduce; the premise is wrong\n")
            failures.append("old design did not reproduce the overtake")

    with connect() as conn, conn.cursor() as cur:
        p0, p2 = run_new_design(cur)
        print("NEW design (single primitive)")
        print(f"  P0 assignee = {p0!r}   P2 assignee = {p2!r}")
        if p0 == 1 and p2 is None:
            print("  -> PASS: P0 took the freed slot, P2 still waiting\n")
        else:
            print("  -> FAIL\n")
            failures.append("new design let P2 overtake P0")

    with connect() as conn, conn.cursor() as cur:
        order = run_priority_ordering_check(cur)
        print("PRIORITY ORDERING (cap 3, four waiting tasks)")
        print(f"  fill order = {order}")
        if order == [301, 302, 303]:
            print("  -> PASS: P0, then P1, then the OLDER of the two P2s\n")
        else:
            print("  -> FAIL: expected [301, 302, 303]\n")
            failures.append(f"drain order was {order}, expected [301, 302, 303]")

    count, p0, p2, errors, outcomes = run_concurrency_check(connect)
    won = outcomes.count("won")
    lost = outcomes.count("lost-race")
    print("CONCURRENCY (16 workers, barrier-synchronised, racing for 1 slot)")
    print(f"  active_task_count = {count} (cap 1)")
    print(f"  P0 assignee = {p0!r}   P2 assignee = {p2!r}")
    print(f"  workers: {won} won, {lost} lost the race, {len(errors)} errored")
    if count > 1:
        failures.append(f"cap exceeded: active_task_count={count}")
    if p0 != 1:
        failures.append("P0 did not win under concurrency")
    if p2 is not None:
        failures.append("P2 was assigned despite the cap")
    if errors:
        failures.append(f"{len(errors)} worker(s) raised: {errors[0]!r}")
    if won != 1:
        failures.append(f"expected exactly 1 winner, got {won}")
    if lost < 1:
        failures.append("no worker lost a race - contention never occurred, "
                        "so this run proves nothing about concurrency")
    print("  -> PASS\n" if not failures else "  -> FAIL\n")

    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
