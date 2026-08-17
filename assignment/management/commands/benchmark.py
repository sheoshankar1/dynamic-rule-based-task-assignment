"""P-3/P-4: plans and latencies for the request-path queries.

Two things are measured and they answer different questions:

  PLANS      -- does any request-path query fall back to a sequential scan?
                A plan regression is silent until the table grows, so this
                asserts rather than reports.

  LATENCY    -- p50/p95 per query, first unloaded, then under a stated number
                of concurrent workers. "200 ms" means nothing without a load
                figure, so the achieved rate is printed next to every number.

Measured at the SQL layer, not through HTTP. That isolates the database cost,
which is what the design claims things about; it excludes WSGI, JSON
serialisation and network, so real end-to-end latency will be higher. Stated
here rather than left for a reader to assume.

    python manage.py benchmark
    python manage.py benchmark --workers 16 --iterations 200
"""

import random
import statistics
import threading
import time

from django.core.management.base import BaseCommand
from django.db import connection, connections

from assignment.models import Rule, RuleEligibleUser, Task, User
from assignment.services import _NEXT_POOLED_TASK, _TOP_CANDIDATE

MY_TASKS = f"""
    SELECT t.id, t.title, t.priority, t.effort_hours, t.status, t.due_date
      FROM {Task._meta.db_table} t
     WHERE t.assignee_id = %s AND t.status NOT IN ('done', 'cancelled')
     ORDER BY t.priority ASC, t.due_date ASC
"""

ELIGIBLE_USERS = f"""
    SELECT u.id, u.username, u.committed_effort_hours, u.lifetime_hours,
           u.active_task_count
      FROM {RuleEligibleUser._meta.db_table} reu
      JOIN {User._meta.db_table} u ON u.id = reu.user_id
     WHERE reu.rule_id = %s AND u.active_task_count < %s
     ORDER BY u.committed_effort_hours ASC, u.lifetime_hours DESC,
              u.date_joined ASC, u.id ASC
     LIMIT 50
"""


class Command(BaseCommand):
    help = "Measure request-path query plans and latencies at seeded scale."

    def add_arguments(self, p):
        p.add_argument("--iterations", type=int, default=200)
        p.add_argument("--workers", type=int, default=8)

    def handle(self, *a, **o):
        rng = random.Random(1)
        rule_ids = list(Rule.objects.values_list("id", flat=True)[:5000])
        user_ids = list(
            Task.objects.filter(assignee__isnull=False)
            .values_list("assignee_id", flat=True)[:5000]
        )
        if not rule_ids or not user_ids:
            self.stderr.write(self.style.ERROR(
                "no seeded data -- run `manage.py seed_scale` first"))
            return

        cases = [
            ("/my-eligible-tasks", MY_TASKS, lambda: [rng.choice(user_ids)]),
            ("/tasks/{id}/eligible-users", ELIGIBLE_USERS,
             lambda: [rng.choice(rule_ids), 5]),
            ("assignment: top candidate", _TOP_CANDIDATE,
             lambda: [rng.choice(rule_ids), 5, 5]),
            ("assignment: next pooled task", _NEXT_POOLED_TASK,
             lambda: [rng.choice(user_ids)]),
        ]

        self._context()
        failures = self._plans(cases)
        self._latency(cases, o["iterations"], o["workers"])

        if failures:
            self.stdout.write(self.style.ERROR(
                f"\nP-3 FAILED: sequential scan on {len(failures)} "
                f"request-path quer{'y' if len(failures) == 1 else 'ies'}: "
                + ", ".join(failures)))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nP-3 PASS: no sequential scan on any request-path query"))

    # -- context -------------------------------------------------------------

    def _context(self):
        with connection.cursor() as cur:
            cur.execute("SHOW random_page_cost")
            (rpc,) = cur.fetchone()
            cur.execute("SELECT version()")
            (version,) = cur.fetchone()
        self.stdout.write(f"{version.split(',')[0]}")
        self.stdout.write(
            f"U={User.objects.count():,}  T={Task.objects.count():,}  "
            f"R={Rule.objects.count():,}  "
            f"eligibility={RuleEligibleUser.objects.count():,}  "
            f"random_page_cost={rpc}")

    # -- plans ---------------------------------------------------------------

    def _plans(self, cases):
        self.stdout.write("\nPLANS")
        failures = []
        with connection.cursor() as cur:
            for name, sql, params in cases:
                cur.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", params())
                plan = "\n".join(r[0] for r in cur.fetchall())
                # A seq scan on a small lookup table is fine; on the large ones
                # it is the regression this check exists to catch.
                bad = [
                    line.strip() for line in plan.splitlines()
                    if "Seq Scan" in line and any(
                        t in line for t in (Task._meta.db_table,
                                            User._meta.db_table,
                                            RuleEligibleUser._meta.db_table))
                ]
                node = next((ln.strip() for ln in plan.splitlines()
                             if "Scan" in ln or "Loop" in ln), "?")
                self.stdout.write(f"  {name:<30} {node[:80]}")
                if bad:
                    failures.append(name)
                    for line in bad:
                        self.stdout.write(self.style.ERROR(f"      {line[:100]}"))
        return failures

    # -- latency -------------------------------------------------------------

    def _latency(self, cases, iterations, workers):
        self.stdout.write(
            f"\nLATENCY  (ms, SQL layer only -- excludes WSGI/JSON/network)")
        self.stdout.write(f"  {'query':<30} {'p50':>8} {'p95':>8} {'max':>8} "
                          f"{'rate':>12}")

        for name, sql, params in cases:
            seq = self._run(sql, params, iterations, workers=1)
            self._row(f"{name} (1 worker)", seq)

        self.stdout.write("")
        for name, sql, params in cases:
            par = self._run(sql, params, iterations, workers=workers)
            self._row(f"{name} ({workers} workers)", par)

    def _run(self, sql, params, iterations, workers):
        latencies, lock = [], threading.Lock()

        def work(n):
            conn = connections.create_connection("default")
            local = []
            try:
                with conn.cursor() as cur:
                    for _ in range(20):                    # warm up
                        cur.execute(sql, params())
                        cur.fetchall()
                    for _ in range(n):
                        t0 = time.perf_counter()
                        cur.execute(sql, params())
                        cur.fetchall()
                        local.append((time.perf_counter() - t0) * 1000)
            finally:
                conn.close()
            with lock:
                latencies.extend(local)

        per = max(iterations // workers, 1)
        started = time.perf_counter()
        threads = [threading.Thread(target=work, args=(per,))
                   for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - started
        return latencies, len(latencies) / wall if wall else 0

    def _row(self, label, result):
        lat, rate = result
        lat.sort()
        p50 = statistics.median(lat)
        p95 = lat[int(len(lat) * 0.95) - 1]
        self.stdout.write(
            f"  {label:<30} {p50:>8.2f} {p95:>8.2f} {max(lat):>8.2f} "
            f"{rate:>9.0f}/s")
