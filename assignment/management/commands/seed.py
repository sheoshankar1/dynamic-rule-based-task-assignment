"""Seed a demonstrable dataset.

Deliberately parameterised on the two unknowns the design is sensitive to
(README section 10): `--rules` controls R, and the predicate mix controls d.
Both are printed at the end rather than buried in code, so a reader can see
what the numbers below were measured against.

    python manage.py seed --users 200 --rules 8 --tasks 50
"""

import random

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import transaction

from assignment import services
from assignment.models import Rule, RuleEligibleUser, Task, User

DEPARTMENTS = ["Finance", "HR", "IT", "Operations"]
LOCATIONS = ["Bangalore", "Pune", "Remote"]


class Command(BaseCommand):
    help = "Seed users, rules and tasks, then let the assigner place them."

    def add_arguments(self, parser):
        parser.add_argument("--users", type=int, default=200)
        parser.add_argument("--rules", type=int, default=8)
        parser.add_argument("--tasks", type=int, default=50)
        parser.add_argument("--seed", type=int, default=42,
                            help="RNG seed; fixed so runs are reproducible")
        parser.add_argument("--if-empty", action="store_true",
                            help="no-op when users already exist, so a container "
                                 "restart does not wipe the database")

    @transaction.atomic
    def handle(self, *args, **opts):
        rng = random.Random(opts["seed"])

        if opts["if_empty"] and User.objects.exists():
            self.stdout.write("users already present, not re-seeding")
            return

        Task.objects.all().delete()
        RuleEligibleUser.objects.all().delete()
        Rule.objects.all().delete()
        User.objects.all().delete()

        manager = User.objects.create_user(
            username="manager", password="manager", role=User.Role.MANAGER,
            department="Operations", experience_years=10, location="Remote",
        )
        User.objects.create_superuser(
            username="admin", password="admin", role=User.Role.ADMIN,
            department="IT", experience_years=10, location="Remote",
        )

        # bulk_create bypasses create_user, so a password must be hashed here or
        # every seeded account is unloggable -- which made "My tasks" impossible
        # to demo. Hashed once and shared: fine for seed data, never for real.
        demo_password = make_password("demo")

        users = [
            User(
                username=f"user{i:04d}",
                password=demo_password,
                department=rng.choice(DEPARTMENTS),
                experience_years=rng.randint(0, 12),
                location=rng.choice(LOCATIONS),
                role=User.Role.USER,
            )
            for i in range(opts["users"])
        ]
        User.objects.bulk_create(users)

        # A small pool of rules, reused across many tasks -- the behaviour D1
        # depends on. Tasks pick from this pool rather than authoring one each.
        rule_specs = [
            {"department": rng.choice(DEPARTMENTS),
             "experience_years": {"gte": rng.choice([0, 2, 4, 6])},
             "max_active_tasks": rng.choice([3, 5, 8])}
            for _ in range(opts["rules"])
        ]
        rules = []
        for spec in rule_specs:
            rule, _ = services.get_or_create_rule(spec)
            services.materialize_rule(rule.id)
            rules.append(rule)

        tasks = []
        for i in range(opts["tasks"]):
            rule = rng.choice(rules)
            tasks.append(Task.objects.create(
                title=f"Task {i:04d}",
                priority=rng.choice([0, 1, 1, 2, 2, 2]),
                effort_hours=rng.choice(["0.5", "1.0", "2.0", "4.0", "8.0"]),
                rule=rule,
                created_by=manager,
            ))

        for task in tasks:
            services.place_task(task.id)

        assigned = Task.objects.filter(assignee__isnull=False).count()
        distinct_rules = Rule.objects.count()

        self.stdout.write(self.style.SUCCESS("seeded"))
        self.stdout.write(f"  users            {User.objects.count()}")
        self.stdout.write(f"  tasks            {len(tasks)}")
        self.stdout.write(f"  distinct rules R {distinct_rules} "
                          f"(requested {opts['rules']}; fewer means dedup fired)")
        self.stdout.write(f"  T/R dedup ratio  {len(tasks) / max(distinct_rules, 1):.1f}:1")
        self.stdout.write(f"  eligibility rows {RuleEligibleUser.objects.count()}")
        self.stdout.write(f"  assigned         {assigned}/{len(tasks)}")
        self.stdout.write(f"  pooled           {len(tasks) - assigned} "
                          f"(no eligible user with free capacity)")
        self.stdout.write("  login            manager/manager, admin/admin, "
                          "any userNNNN/demo")
