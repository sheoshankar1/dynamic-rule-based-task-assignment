"""End-to-end HTTP latency and sustainable throughput.

The SQL-layer benchmark isolates database cost, which is what the design makes
claims about, but it excludes WSGI, DRF serialisation and the network. This
measures the whole request as a client experiences it.

It also answers a question the brief leaves open. "Under 200 ms" is meaningless
without a load figure and the brief supplies none, so rather than inventing a
target this finds the rate the system actually sustains while staying inside
the budget -- a measured capacity instead of an asserted one.

    python manage.py benchmark_http --duration 10 --workers 8
    python manage.py benchmark_http --find-capacity
"""

import json
import statistics
import threading
import time
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand

BUDGET_MS = 200


class Command(BaseCommand):
    help = "Measure end-to-end HTTP latency and sustainable throughput."

    def add_arguments(self, p):
        p.add_argument("--base", default="http://localhost:8000")
        p.add_argument("--username", default="manager")
        p.add_argument("--password", default="manager")
        p.add_argument("--duration", type=int, default=8,
                       help="seconds per measured phase")
        p.add_argument("--workers", type=int, default=8)
        p.add_argument("--find-capacity", action="store_true",
                       help="ramp concurrency until p95 exceeds the budget")
        p.add_argument("--ramp-create", action="store_true",
                       help="ramp POST /tasks/ specifically; creation carries "
                            "the same budget and is the write path")

    # -- plumbing -----------------------------------------------------------

    def _call(self, base, path, token=None, body=None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": "Bearer " + token} if token else {}),
            },
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read() or b"null")

    def _login(self, base, username, password):
        return self._call(base, "/auth/login",
                          body={"username": username, "password": password})["access"]

    def _drive(self, base, token, path, seconds, workers, body_factory=None):
        """Hold `workers` concurrent clients for `seconds`, return latencies.

        `body_factory(i)` makes this usable for writes as well as reads -- task
        creation is a request-path operation with the same latency budget, and
        measuring only reads would leave the busiest code path unmeasured.
        """
        latencies, errors, lock = [], [], threading.Lock()
        stop = time.perf_counter() + seconds

        def run():
            local, n = [], 0
            while time.perf_counter() < stop:
                body = body_factory(n) if body_factory else None
                n += 1
                t0 = time.perf_counter()
                try:
                    self._call(base, path, token, body)
                    local.append((time.perf_counter() - t0) * 1000)
                except (urllib.error.URLError, TimeoutError) as exc:
                    with lock:
                        errors.append(repr(exc))
            with lock:
                latencies.extend(local)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started
        return latencies, errors, len(latencies) / elapsed if elapsed else 0

    @staticmethod
    def _pct(values, q):
        values = sorted(values)
        return values[min(int(len(values) * q), len(values) - 1)]

    # -- phases -------------------------------------------------------------

    def handle(self, *a, **o):
        base, workers = o["base"], o["workers"]
        try:
            token = self._login(base, o["username"], o["password"])
        except Exception as exc:
            self.stderr.write(self.style.ERROR(
                f"cannot reach {base}: {exc}. Is the stack running?"))
            return

        # Discover a live task id rather than assuming one. Reseeding does not
        # reset the sequence, so a hardcoded id silently measures 404s.
        listing = self._call(base, "/tasks/?limit=1", token)
        endpoints = [
            ("GET /my-eligible-tasks", "/my-eligible-tasks"),
            ("GET /tasks/?limit=50", "/tasks/?limit=50"),
        ]
        if listing:
            endpoints.append(
                (f"GET /tasks/{{id}}", f"/tasks/{listing[0]['id']}"))
        else:
            self.stdout.write(self.style.WARNING(
                "  no tasks found - skipping the detail endpoint"))

        self.stdout.write(self.style.SUCCESS(
            f"\nEND-TO-END HTTP  (includes WSGI, DRF serialisation and network)"))
        self.stdout.write(f"  {base}   {workers} concurrent clients, "
                          f"{o['duration']}s per endpoint\n")
        self.stdout.write(f"  {'endpoint':<26}{'p50':>9}{'p95':>9}{'p99':>9}"
                          f"{'max':>9}{'rate':>12}{'errors':>8}")

        # Task creation is on the request path and carries the same budget, so
        # it is measured too -- both against a rule that already exists (the
        # common case, no materialisation) and against a brand-new fingerprint.
        DEPTS = ["Finance", "HR", "IT", "Operations"]
        endpoints += [
            ("POST /tasks/ (rule reused)", "/tasks/",
             lambda i: {"title": f"bench {i}", "priority": 2,
                        "effort_hours": "1.0",
                        "rules": {"department": "Finance",
                                  "max_active_tasks": 5}}),
            ("POST /tasks/ (new rule)", "/tasks/",
             lambda i: {"title": f"bench new {i}", "priority": 2,
                        "effort_hours": "1.0",
                        "rules": {"department": DEPTS[i % 4],
                                  "experience_years": {"gte": (i * 7) % 13},
                                  "max_active_tasks": 5}}),
        ]

        for entry in endpoints:
            label, path = entry[0], entry[1]
            factory = entry[2] if len(entry) > 2 else None
            self._drive(base, token, path, 1, workers, factory)   # warm
            lat, errs, rate = self._drive(base, token, path,
                                          o["duration"], workers, factory)
            if not lat:
                reason = errs[0] if errs else "unknown"
                self.stdout.write(self.style.ERROR(
                    f"  {label:<26} no successful requests: {reason}"))
                continue
            self.stdout.write(
                f"  {label:<26}{statistics.median(lat):>8.1f}ms"
                f"{self._pct(lat, 0.95):>8.1f}ms{self._pct(lat, 0.99):>8.1f}ms"
                f"{max(lat):>8.1f}ms{rate:>9.0f}/s{len(errs):>8}")

        if o["find_capacity"]:
            self._find_capacity(base, token, o["duration"])
        if o["ramp_create"]:
            self._ramp_create(base, token, o["duration"])

        self.stdout.write("")

    def _find_capacity(self, base, token, duration):
        """Ramp concurrency until p95 leaves the budget.

        The brief gives a latency target with no load figure. This reports the
        load at which the target stops holding, which is the useful form of the
        same statement.
        """
        path = "/my-eligible-tasks"
        self.stdout.write(self.style.SUCCESS(
            f"\nCAPACITY  (ramping until p95 exceeds {BUDGET_MS}ms on {path})"))
        self.stdout.write(f"  {'clients':>9}{'p95':>10}{'rate':>12}{'errors':>8}")

        best = None
        for clients in (1, 2, 4, 8, 16, 32, 64, 128):
            lat, errs, rate = self._drive(base, token, path, duration, clients)
            if not lat:
                self.stdout.write(f"  {clients:>9}   no successful requests")
                break
            p95 = self._pct(lat, 0.95)
            flag = "" if p95 <= BUDGET_MS and not errs else "   <- budget exceeded"
            self.stdout.write(
                f"  {clients:>9}{p95:>9.1f}ms{rate:>9.0f}/s{len(errs):>8}{flag}")
            if p95 <= BUDGET_MS and not errs:
                best = (clients, rate, p95)
            else:
                break

        if best:
            self.stdout.write(self.style.SUCCESS(
                f"\n  Sustains {best[1]:.0f} req/s at {best[0]} concurrent "
                f"clients with p95 {best[2]:.1f}ms, inside the {BUDGET_MS}ms budget."))
        else:
            self.stdout.write(self.style.WARNING(
                "\n  The budget was exceeded at the lowest concurrency tested."))

    def _ramp_create(self, base, token, duration):
        """Find the concurrency at which task creation leaves the budget.

        Creation is the write path and carries the same 200ms target. Reported
        as a ramp rather than a single number because a latency figure without
        its concurrency says nothing.
        """
        self.stdout.write(self.style.SUCCESS(
            f"\nCREATION RAMP  (POST /tasks/, budget {BUDGET_MS}ms)"))
        self.stdout.write(f"  {'clients':>9}{'p50':>10}{'p95':>10}{'rate':>11}"
                          f"{'errors':>8}")

        body = lambda i: {"title": f"ramp {i}", "priority": 2,
                          "effort_hours": "1.0",
                          "rules": {"department": "Finance",
                                    "max_active_tasks": 5}}
        best = None
        for clients in (1, 2, 4, 8, 16):
            lat, errs, rate = self._drive(base, token, "/tasks/", duration,
                                          clients, body)
            if not lat:
                self.stdout.write(f"  {clients:>9}  no successful requests")
                break
            p50, p95 = statistics.median(lat), self._pct(lat, 0.95)
            ok = p95 <= BUDGET_MS and not errs
            self.stdout.write(
                f"  {clients:>9}{p50:>9.1f}ms{p95:>9.1f}ms{rate:>8.0f}/s"
                f"{len(errs):>8}{'' if ok else '   <- over budget'}")
            if ok:
                best = (clients, rate, p95)
        if best:
            self.stdout.write(self.style.SUCCESS(
                f"\n  Creation stays inside {BUDGET_MS}ms up to {best[0]} "
                f"concurrent authors: {best[1]:.0f} tasks/s at p95 {best[2]:.1f}ms."))
        else:
            self.stdout.write(self.style.WARNING(
                f"\n  Creation exceeded {BUDGET_MS}ms even at one client."))
