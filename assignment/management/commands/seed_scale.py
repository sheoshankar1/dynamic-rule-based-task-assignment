"""P-1/P-2: seed at the brief's stated scale, parameterised on R and d.

Separate from `seed`, and deliberately so. `seed` drives the real service layer
and proves the pipeline works; it cannot be used here because placing 1M tasks
one at a time through `place_task` would take hours. This command writes the
*outcome* of assignment directly in bulk SQL and then reconciles the denormalised
counters -- so it produces a realistic resting state, not a proof that the
assigner works. Do not read a successful run here as evidence of correctness.

R and d are the two unknowns README section 10 is sensitive to, so both are CLI
parameters and both are reported as *measured* afterwards rather than assumed.

    python manage.py seed_scale --users 100000 --tasks 1000000 --rules 1000
    python manage.py seed_scale --degenerate      # R = T, the section 10 floor
"""

import time

from django.core.management.base import BaseCommand
from django.db import connection

from assignment.models import Rule, RuleEligibleUser, Task, User

DEPARTMENTS = ["Finance", "HR", "IT", "Operations"]
LOCATIONS = ["Bangalore", "Pune", "Delhi", "Remote"]


class Command(BaseCommand):
    help = "Seed 100k users / 1M tasks for benchmarking."

    def add_arguments(self, p):
        p.add_argument("--users", type=int, default=100_000)
        p.add_argument("--tasks", type=int, default=1_000_000)
        p.add_argument("--rules", type=int, default=1_000)
        p.add_argument("--open-pct", type=int, default=10,
                       help="share of tasks still open; the rest are done")
        p.add_argument("--degenerate", action="store_true",
                       help="R = T: one unique rule per task, the case where "
                            "per-rule materialisation collapses into per-task")

    def handle(self, *a, **o):
        t0 = time.time()
        n_users, n_tasks = o["users"], o["tasks"]
        n_rules = n_tasks if o["degenerate"] else o["rules"]

        if o["degenerate"] and n_tasks > 50_000:
            self.stderr.write(self.style.WARNING(
                "degenerate mode with >50k tasks will materialise R*d*U rows; "
                "capping tasks at 50000"))
            n_tasks = n_rules = 50_000

        with connection.cursor() as cur:
            self._wipe(cur)
            self._users(cur, n_users)
            self._rules(cur, n_rules)
            self._materialise(cur)
            self._tasks(cur, n_tasks, o["open_pct"])
            self._assign(cur)
            self._reconcile_counters(cur)
            stats = self._stats(cur, n_users)

        self._report(stats, n_rules, time.time() - t0)

    # -- steps --------------------------------------------------------------

    def _wipe(self, cur):
        cur.execute(
            f"TRUNCATE {Task._meta.db_table}, "
            f"{RuleEligibleUser._meta.db_table}, {Rule._meta.db_table}, "
            f"{User._meta.db_table} RESTART IDENTITY CASCADE"
        )

    def _users(self, cur, n):
        self.stdout.write(f"users: {n}")
        cur.execute(
            f"""
            INSERT INTO {User._meta.db_table}
                (id, password, is_superuser, username, first_name, last_name,
                 email, is_staff, is_active, date_joined, role, department,
                 experience_years, location, active_task_count,
                 committed_effort_hours, lifetime_hours, stable_attrs_version)
            SELECT i, '', false, 'user' || i, '', '', '', false, true,
                   timestamptz '2020-01-01' + (i %% 1500) * interval '1 day',
                   'user',
                   (ARRAY{DEPARTMENTS})[1 + (i %% 4)],
                   i %% 13,
                   (ARRAY{LOCATIONS})[1 + (i %% 4)],
                   0, 0, 0, 0
              FROM generate_series(1, %s) AS i
            """,
            [n],
        )

    def _rules(self, cur, n):
        """Rules spread across the plausible selectivity range.

        Every rule pins a department (d <= 0.25 by construction) and most add an
        experience floor, so d varies from roughly 0.02 to 0.25 -- the range the
        section 10 sensitivity table covers.
        """
        self.stdout.write(f"rules: {n}")
        cur.execute(
            f"""
            INSERT INTO {Rule._meta.db_table}
                (id, fingerprint, predicates, max_active_tasks, eligible_count,
                 materialized_at)
            SELECT i,
                   md5('rule' || i),
                   jsonb_build_object(
                       'department',
                       (ARRAY{DEPARTMENTS})[1 + (i %% 4)],
                       'experience_years',
                       jsonb_build_object('gte', (i %% 6) * 2)
                   ),
                   5, NULL, NULL
              FROM generate_series(1, %s) AS i
            """,
            [n],
        )

    def _materialise(self, cur):
        """One statement for every rule at once.

        `services.materialize_rule` runs the same predicate per rule; doing it
        set-wise here is a seeding shortcut, not a change to how the system
        materialises at runtime.
        """
        self.stdout.write("eligibility rows...")
        cur.execute(
            f"""
            INSERT INTO {RuleEligibleUser._meta.db_table} (rule_id, user_id)
            SELECT r.id, u.id
              FROM {Rule._meta.db_table} r
              JOIN {User._meta.db_table} u
                ON u.department = (r.predicates ->> 'department')
               AND u.experience_years >=
                   (r.predicates -> 'experience_years' ->> 'gte')::int
            """
        )
        cur.execute(
            f"""
            UPDATE {Rule._meta.db_table} r
               SET eligible_count = c.n, materialized_at = now()
              FROM (SELECT rule_id, count(*) n
                      FROM {RuleEligibleUser._meta.db_table} GROUP BY rule_id) c
             WHERE r.id = c.rule_id
            """
        )

    def _tasks(self, cur, n, open_pct):
        self.stdout.write(f"tasks: {n}")
        cur.execute(f"SELECT count(*) FROM {Rule._meta.db_table}")
        (n_rules,) = cur.fetchone()
        cur.execute(
            f"""
            INSERT INTO {Task._meta.db_table}
                (id, title, description, due_date, priority, effort_hours,
                 status, rule_id, created_by_id, assignee_id, created_at)
            SELECT i, 'Task ' || i, '', NULL,
                   (ARRAY[0,1,1,2,2,2])[1 + (i %% 6)],
                   (ARRAY[0.5,1,2,4,8])[1 + (i %% 5)],
                   -- distinct primes: `i %% 100` and `i %% n_rules` are NOT
                   -- independent, so deriving both status and rule from raw i
                   -- correlates them -- every task of a given rule lands with
                   -- the same status. Measured: 10%% of rules held 100%% of the
                   -- open work and the cap filter became meaningless.
                   CASE WHEN (i::bigint * 104729) %% 100 < %s THEN 'todo' ELSE 'done' END,
                   1 + ((i::bigint * 7919) %% %s),
                   1,
                   NULL,
                   now() - (i %% 5000) * interval '1 minute'
              FROM generate_series(1, %s) AS i
            """,
            [open_pct, n_rules, n],
        )

    def _assign(self, cur):
        """Spread tasks deterministically across each rule's eligible set.

        Naively this is a window function over every eligibility row joined to
        every task on `t.id % n` -- which ranks ~15M rows and then hash-joins on
        a computed key. Measured at >16 minutes and cancelled.

        Instead: rank once, keep only the first PICK users per rule, and join
        1M tasks against that small indexed table. Load still spreads across
        PICK distinct users per rule, which is all the benchmark needs; it is
        not a claim about how the real assigner distributes work.
        """
        PICK = 400
        self.stdout.write(f"assigning (spread over <={PICK} users/rule)...")
        cur.execute("DROP TABLE IF EXISTS pick")
        cur.execute(
            f"""
            CREATE TEMP TABLE pick AS
            SELECT s.rule_id, s.user_id, s.rn,
                   LEAST(r.eligible_count, {PICK}) AS cnt
              FROM (SELECT rule_id, user_id,
                           row_number() OVER (PARTITION BY rule_id
                                              ORDER BY (user_id * 7919 + rule_id) % 1000003) - 1 AS rn
                      FROM {RuleEligibleUser._meta.db_table}) s
              JOIN {Rule._meta.db_table} r ON r.id = s.rule_id
             WHERE s.rn < {PICK}
            """
        )
        cur.execute("CREATE INDEX ON pick (rule_id, rn)")
        cur.execute("ANALYZE pick")
        cur.execute(
            f"""
            UPDATE {Task._meta.db_table} t
               SET assignee_id = p.user_id
              FROM pick p
             WHERE p.rule_id = t.rule_id
               AND p.rn = t.id % p.cnt
            """
        )

    def _reconcile_counters(self, cur):
        """Rebuild the denormalised columns from the tasks table.

        This is the same reconciliation SP-2 would run in production to detect
        drift -- here it is the source of truth rather than a check.
        """
        self.stdout.write("reconciling counters...")
        cur.execute(
            f"""
            UPDATE {User._meta.db_table} u
               SET active_task_count      = COALESCE(c.n, 0),
                   committed_effort_hours = COALESCE(c.h, 0)
              FROM (SELECT assignee_id, count(*) n, sum(effort_hours) h
                      FROM {Task._meta.db_table}
                     WHERE status NOT IN ('done', 'cancelled')
                       AND assignee_id IS NOT NULL
                     GROUP BY assignee_id) c
             WHERE u.id = c.assignee_id
            """
        )
        cur.execute(
            f"""
            UPDATE {User._meta.db_table} u
               SET lifetime_hours = COALESCE(c.h, 0)
              FROM (SELECT assignee_id, sum(effort_hours) h
                      FROM {Task._meta.db_table}
                     WHERE status = 'done' AND assignee_id IS NOT NULL
                     GROUP BY assignee_id) c
             WHERE u.id = c.assignee_id
            """
        )
        cur.execute("ANALYZE")

    def _stats(self, cur, n_users):
        cur.execute(f"""
            SELECT (SELECT count(*) FROM {User._meta.db_table}),
                   (SELECT count(*) FROM {Task._meta.db_table}),
                   (SELECT count(*) FROM {Rule._meta.db_table}),
                   (SELECT count(*) FROM {RuleEligibleUser._meta.db_table}),
                   (SELECT max(active_task_count) FROM {User._meta.db_table}),
                   (SELECT count(*) FROM {User._meta.db_table}
                     WHERE active_task_count < 5),
                   (SELECT max(eligible_count) FROM {Rule._meta.db_table}),
                   (SELECT min(eligible_count) FROM {Rule._meta.db_table}),
                   pg_size_pretty(pg_total_relation_size(
                       '{RuleEligibleUser._meta.db_table}'))
        """)
        return cur.fetchone()

    def _report(self, s, n_rules, secs):
        users, tasks, rules, elig, max_open, under_cap, max_e, min_e, size = s
        self.stdout.write(self.style.SUCCESS(f"\nseeded in {secs:.1f}s"))
        self.stdout.write(f"  U  users            {users:,}")
        self.stdout.write(f"  T  tasks            {tasks:,}")
        self.stdout.write(f"  R  distinct rules   {rules:,}")
        self.stdout.write(f"     T/R ratio        {tasks / max(rules, 1):,.0f}:1")
        self.stdout.write(f"  d  measured         {min_e / users:.3f} .. {max_e / users:.3f}")
        self.stdout.write(f"     eligibility rows {elig:,}  ({size})")
        self.stdout.write(f"     bytes/row        {_bytes_per_row(size, elig)}")
        self.stdout.write(f"     max open/user    {max_open}")
        self.stdout.write(
            f"     users under cap  {under_cap:,} / {users:,} "
            f"({100 * under_cap / max(users, 1):.0f}%)")


def _bytes_per_row(size_pretty, rows):
    units = {"bytes": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
    try:
        value, unit = size_pretty.split()
        return f"{float(value) * units[unit] / max(rows, 1):.0f}"
    except (ValueError, KeyError):
        return "?"
