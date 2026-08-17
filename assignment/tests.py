"""Tests for the parts where being wrong is invisible.

Not CRUD serialisers -- those fail loudly and cheaply. These cover the rule
engine's two evaluators, materialisation, the assignment ladder, the priority
contract, and the completion bookkeeping.

Run:
    POSTGRES_DB=taskassign .venv/bin/python manage.py test assignment -v2
"""

import itertools
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from django.utils import timezone

from . import services, signals, views
from .models import Rule, RuleEligibleUser, Task, User
from .rules import InvalidRule, canonicalize, fingerprint, matches, split, to_sql


def make_user(username, dept="Finance", years=5, location="Bangalore", **kw):
    return User.objects.create_user(
        username=username, password="x", department=dept,
        experience_years=years, location=location, **kw
    )


class RuleCanonicalisationTests(TestCase):
    def test_absent_and_empty_predicates_hash_identically(self):
        """If these diverge, deduplication silently degrades toward per-task
        materialisation with nothing failing loudly."""
        a, _ = split({"department": "Finance"})
        b, _ = split({"department": "Finance", "location": ""})
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_key_order_does_not_matter(self):
        a, _ = split({"department": "IT", "experience_years": {"gte": 3}})
        b, _ = split({"experience_years": {"gte": 3}, "department": "IT"})
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_a_single_element_list_is_accepted_and_flattened(self):
        """Earlier clients sent lists; they still work and hash identically."""
        a, _ = split({"department": ["HR"]})
        b, _ = split({"department": "HR"})
        self.assertEqual(a, {"department": "HR"})
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_a_repeated_value_is_still_one_value(self):
        a, _ = split({"department": ["HR", "HR"]})
        self.assertEqual(a, {"department": "HR"})

    def test_multi_valued_predicates_are_rejected_not_truncated(self):
        """Silently dropping a department would route work to the wrong team
        with nothing to show for it."""
        for bad in ({"department": ["IT", "HR"]}, {"location": ["Pune", "Delhi"]}):
            with self.assertRaises(InvalidRule) as ctx:
                split(bad)
            self.assertIn("single value", str(ctx.exception))

    def test_cap_is_outside_the_fingerprint(self):
        """Two rules differing only in cap must share one materialised set."""
        a, va = split({"department": "Finance", "max_active_tasks": 5})
        b, vb = split({"department": "Finance", "max_active_tasks": 9})
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertEqual((va["max_active_tasks"], vb["max_active_tasks"]), (5, 9))

    def test_malformed_rules_are_rejected_at_the_trust_boundary(self):
        for bad in (
            {"nonsense": 1},
            {"experience_years": {"between": 3}},
            {"experience_years": {"gte": "four"}},
            {"max_active_tasks": 0},
            {"max_active_tasks": True},
            "not-a-dict",
        ):
            with self.assertRaises(InvalidRule):
                split(bad)


class EvaluatorEquivalenceTests(TestCase):
    """R-5: to_sql and matches are two implementations of one semantics.

    Two implementations is a real divergence risk, so this is the test that
    makes the duplication safe. It is exhaustive over a small generated space
    rather than sampled, so it cannot pass by luck.
    """

    @classmethod
    def setUpTestData(cls):
        for i, (dept, years, loc) in enumerate(itertools.product(
            ["Finance", "HR", "IT", "Operations"],
            [0, 3, 4, 7],
            ["Bangalore", "Remote"],
        )):
            make_user(f"u{i}", dept=dept, years=years, location=loc)

    def test_sql_and_python_select_the_same_users(self):
        candidates = [
            {},
            {"department": "Finance"},
            {"department": "IT"},
            {"experience_years": {"gte": 4}},
            {"experience_years": {"lte": 3}},
            {"experience_years": {"gte": 3, "lte": 7}},
            {"location": "Remote"},
            {"department": "HR", "experience_years": {"gte": 4}},
            {"department": "IT", "location": "Bangalore",
             "experience_years": {"gt": 0}},
        ]
        users = list(User.objects.all())
        for predicates in candidates:
            with self.subTest(predicates=predicates):
                where, params = to_sql(predicates, alias="u")
                sql_ids = set(
                    User.objects.extra(where=[where.replace("u.", "assignment_user.")],
                                       params=params).values_list("id", flat=True)
                )
                py_ids = {u.id for u in users if matches(predicates, u)}
                self.assertEqual(sql_ids, py_ids)


class MaterialisationTests(TestCase):
    def test_eligible_set_matches_the_predicate(self):
        make_user("fin_senior", dept="Finance", years=6)
        make_user("fin_junior", dept="Finance", years=2)
        make_user("hr_senior", dept="HR", years=6)

        rule, _ = services.get_or_create_rule(
            {"department": "Finance", "experience_years": {"gte": 4},
             "max_active_tasks": 5}
        )
        count = services.materialize_rule(rule.id)

        self.assertEqual(count, 1)
        rule.refresh_from_db()
        self.assertEqual(rule.eligible_count, 1)
        self.assertEqual(
            list(RuleEligibleUser.objects.filter(rule=rule)
                 .values_list("user__username", flat=True)),
            ["fin_senior"],
        )

    def test_identical_rule_is_reused_not_recomputed(self):
        """D1: the common case for a rule edit is zero work."""
        rule_a, created_a = services.get_or_create_rule({"department": "IT"})
        rule_b, created_b = services.get_or_create_rule({"department": "IT"})
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(rule_a.id, rule_b.id)
        self.assertEqual(Rule.objects.count(), 1)

    def test_recompute_user_writes_only_the_delta(self):
        user = make_user("mover", dept="HR", years=2)
        junior_hr, _ = services.get_or_create_rule(
            {"department": "HR", "experience_years": {"lte": 3}})
        senior_hr, _ = services.get_or_create_rule(
            {"department": "HR", "experience_years": {"gte": 4}})
        for r in (junior_hr, senior_hr):
            services.materialize_rule(r.id)

        self.assertEqual(
            set(RuleEligibleUser.objects.filter(user=user)
                .values_list("rule_id", flat=True)), {junior_hr.id})

        user.experience_years = 6
        user.save(update_fields=["experience_years"])
        delta = services.recompute_user(user.id)

        self.assertEqual(delta, {"added": [senior_hr.id], "removed": [junior_hr.id]})


class AssignmentLadderTests(TestCase):
    """The four-key ladder, one key at a time."""

    def setUp(self):
        self.manager = make_user(
            "mgr", role=User.Role.MANAGER, dept="Operations"
        )
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})

    def _task(self, priority=2, effort="1.0"):
        return Task.objects.create(
            title="t", priority=priority, effort_hours=Decimal(effort),
            rule=self.rule, created_by=self.manager,
        )

    def test_key1_least_committed_hours_wins(self):
        busy = make_user("busy")
        idle = make_user("idle")
        User.objects.filter(pk=busy.pk).update(committed_effort_hours=Decimal("8"))
        services.materialize_rule(self.rule.id)

        task = self._task()
        self.assertEqual(services.place_task(task.id), idle.id)

    def test_key2_more_lifetime_hours_breaks_a_load_tie(self):
        proven = make_user("proven")
        novice = make_user("novice")
        User.objects.filter(pk=proven.pk).update(lifetime_hours=Decimal("500"))
        User.objects.filter(pk=novice.pk).update(lifetime_hours=Decimal("10"))
        services.materialize_rule(self.rule.id)

        task = self._task()
        self.assertEqual(services.place_task(task.id), proven.id)

    def test_key4_id_makes_the_order_total(self):
        """Without a unique final key the winner is engine-chosen. Here every
        earlier key ties exactly, so only `id` can decide."""
        first = make_user("tie_a")
        second = make_user("tie_b")
        User.objects.filter(pk__in=[first.pk, second.pk]).update(
            committed_effort_hours=Decimal("0"),
            lifetime_hours=Decimal("0"),
            date_joined=first.date_joined,
        )
        services.materialize_rule(self.rule.id)

        winner = services.place_task(self._task().id)
        self.assertEqual(winner, min(first.id, second.id))

    def test_capacity_cap_is_respected(self):
        only = make_user("only")
        User.objects.filter(pk=only.pk).update(active_task_count=5)
        services.materialize_rule(self.rule.id)

        task = self._task()
        self.assertIsNone(services.place_task(task.id))
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)


class PriorityContractTests(TestCase):
    """A waiting P0 must win the next freed slot -- the whole reason there is a
    single assignment primitive.

    This is the scenario from tests/test_s7_priority_overtake.py, re-run against
    the real service layer rather than hand-written SQL.
    """

    def setUp(self):
        self.manager = make_user(
            "mgr", role=User.Role.MANAGER, dept="Operations"
        )
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 1})
        self.worker = make_user("worker")
        services.materialize_rule(self.rule.id)

    def _task(self, priority, effort="1.0"):
        return Task.objects.create(
            title=f"P{priority}", priority=priority, effort_hours=Decimal(effort),
            rule=self.rule, created_by=self.manager,
        )

    def test_waiting_p0_beats_a_newly_created_p2(self):
        occupying = self._task(2)
        services.place_task(occupying.id)
        occupying.refresh_from_db()
        self.assertEqual(occupying.assignee_id, self.worker.id)

        waiting_p0 = self._task(0)                      # no capacity: pools
        self.assertIsNone(services.place_task(waiting_p0.id))

        services.complete_task(occupying.id)            # slot frees
        new_p2 = self._task(2)                          # arrives in the window
        services.place_task(new_p2.id)                  # adversarial ordering
        services.fill_capacity(self.worker.id)

        waiting_p0.refresh_from_db()
        new_p2.refresh_from_db()
        self.assertEqual(waiting_p0.assignee_id, self.worker.id)
        self.assertIsNone(new_p2.assignee_id)

    def test_drain_order_is_priority_then_age(self):
        User.objects.filter(pk=self.worker.pk).update(active_task_count=0)
        Rule.objects.filter(pk=self.rule.pk).update(max_active_tasks=3)

        older_p2 = self._task(2)
        p0 = self._task(0)
        p1 = self._task(1)
        newer_p2 = self._task(2)

        assigned = services.fill_capacity(self.worker.id)

        self.assertEqual(assigned, [p0.id, p1.id, older_p2.id])
        newer_p2.refresh_from_db()
        self.assertIsNone(newer_p2.assignee_id)


class CompletionTests(TestCase):
    def setUp(self):
        self.manager = make_user(
            "mgr", role=User.Role.MANAGER, dept="Operations"
        )
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})
        self.worker = make_user("worker")
        services.materialize_rule(self.rule.id)
        self.task = Task.objects.create(
            title="t", priority=1, effort_hours=Decimal("4.50"),
            rule=self.rule, created_by=self.manager,
        )
        services.place_task(self.task.id)

    def test_completion_moves_effort_to_the_track_record(self):
        services.complete_task(self.task.id)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.active_task_count, 0)
        self.assertEqual(self.worker.committed_effort_hours, Decimal("0.00"))
        self.assertEqual(self.worker.lifetime_hours, Decimal("4.50"))

    def test_cancellation_frees_the_slot_without_crediting_lifetime(self):
        """Crediting undelivered work would corrupt selection key 2 silently
        and permanently."""
        services.complete_task(self.task.id, cancelled=True)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.active_task_count, 0)
        self.assertEqual(self.worker.committed_effort_hours, Decimal("0.00"))
        self.assertEqual(self.worker.lifetime_hours, Decimal("0.00"))

    def test_completing_twice_is_a_no_op(self):
        services.complete_task(self.task.id)
        self.assertIsNone(services.complete_task(self.task.id))
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.lifetime_hours, Decimal("4.50"))


class StableAttributeSignalTests(TestCase):
    """E-5/E-6: eligibility follows stable attributes, and only those."""

    def setUp(self):
        cache.clear()
        self.user = make_user("mover", dept="HR", years=2)
        self.senior_hr, _ = services.get_or_create_rule(
            {"department": "HR", "experience_years": {"gte": 4}})
        services.materialize_rule(self.senior_hr.id)

    def test_volatile_change_triggers_no_recompute(self):
        """D2 is meaningless if this fails: these columns move on every single
        assignment and completion."""
        with mock.patch.object(signals, "schedule_recompute") as sched:
            u = User.objects.get(pk=self.user.pk)
            u.active_task_count = 3
            u.committed_effort_hours = Decimal("7.5")
            u.save(update_fields=["active_task_count", "committed_effort_hours"])
        sched.assert_not_called()

    def test_stable_change_triggers_recompute(self):
        with mock.patch.object(signals, "schedule_recompute") as sched:
            u = User.objects.get(pk=self.user.pk)
            u.experience_years = 6
            u.save(update_fields=["experience_years"])
        sched.assert_called_once_with(self.user.pk)

    def test_saving_a_stable_field_to_its_current_value_is_not_a_change(self):
        with mock.patch.object(signals, "schedule_recompute") as sched:
            u = User.objects.get(pk=self.user.pk)
            u.department = "HR"                       # unchanged
            u.save(update_fields=["department"])
        sched.assert_not_called()

    def test_burst_of_edits_schedules_one_recompute(self):
        """captureOnCommitCallbacks is required, not incidental: the enqueue is
        deliberately deferred to transaction.on_commit so a worker can never
        pick the job up and read state the transaction has not committed yet.
        Inside TestCase's rollback wrapper that commit never happens."""
        # setUp created a user, which legitimately scheduled its own recompute
        # and left the debounce key live. Clear it so this asserts on the burst.
        cache.clear()
        with mock.patch.object(signals, "tasks_module") as tasks_mod:
            with self.captureOnCommitCallbacks(execute=True):
                for years in (4, 5, 6, 7, 8):
                    u = User.objects.get(pk=self.user.pk)
                    u.experience_years = years
                    u.save(update_fields=["experience_years"])
        self.assertEqual(
            tasks_mod.return_value.recompute_user.apply_async.call_count, 1
        )

    def test_eligibility_actually_follows_the_change(self):
        u = User.objects.get(pk=self.user.pk)
        u.experience_years = 6
        u.save(update_fields=["experience_years"])
        services.recompute_user(u.pk)               # what the debounced job runs
        self.assertTrue(
            RuleEligibleUser.objects.filter(
                rule=self.senior_hr, user=self.user).exists()
        )


class PoolRecoveryTests(TestCase):
    """A-7 / A-10: the two operational safety nets."""

    def setUp(self):
        self.manager = make_user("mgr", role=User.Role.MANAGER, dept="Operations")
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})
        services.materialize_rule(self.rule.id)

    def _pooled_task(self, priority=2):
        return Task.objects.create(
            title="orphan", priority=priority, effort_hours=Decimal("1.0"),
            rule=self.rule, created_by=self.manager,
        )

    def test_sweep_places_a_task_the_event_path_missed(self):
        """Simulates a dropped queue message: the task exists and a user is
        eligible, but no trigger ever fired."""
        worker = make_user("worker")
        services.materialize_rule(self.rule.id)
        task = self._pooled_task()                   # deliberately not placed

        self.assertEqual(services.sweep_unassigned_pool(), [task.id])
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, worker.id)

    def test_sweep_is_a_no_op_when_the_event_path_worked(self):
        make_user("worker")
        services.materialize_rule(self.rule.id)
        task = self._pooled_task()
        services.place_task(task.id)

        self.assertEqual(services.sweep_unassigned_pool(), [])

    def test_structurally_stuck_task_is_reported(self):
        """Nobody matches this rule, so it can sit forever. A task nobody can
        see is indistinguishable from one that was never created."""
        task = self._pooled_task()
        Task.objects.filter(pk=task.pk).update(
            created_at=timezone.now() - timedelta(days=2))

        with self.assertLogs("assignment.services", level="WARNING") as logged:
            self.assertEqual(services.flag_stuck_tasks(), [task.id])
        self.assertIn(f"task {task.id}", logged.output[0])
        self.assertIn("unassignable", logged.output[0])

    def test_a_young_pooled_task_is_not_flagged(self):
        self._pooled_task()
        self.assertEqual(services.flag_stuck_tasks(), [])


class RuleRepointTests(TestCase):
    """E-7 / Story 4: rules are immutable; editing swaps a foreign key."""

    def setUp(self):
        self.manager = make_user("mgr", role=User.Role.MANAGER, dept="Operations")
        self.finance, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})
        services.materialize_rule(self.finance.id)
        self.fin_user = make_user("fin", dept="Finance")
        self.hr_user = make_user("hr", dept="HR")
        services.materialize_rule(self.finance.id)
        self.task = Task.objects.create(
            title="t", priority=1, effort_hours=Decimal("2.0"),
            rule=self.finance, created_by=self.manager,
        )
        services.place_task(self.task.id)

    def test_editing_to_an_existing_fingerprint_costs_no_recompute(self):
        hr_rule, _ = services.get_or_create_rule(
            {"department": "HR", "max_active_tasks": 5})
        services.materialize_rule(hr_rule.id)

        rule, created, _ = services.repoint_rule(
            self.task.id, {"department": "HR", "max_active_tasks": 5})
        self.assertFalse(created)
        self.assertEqual(rule.id, hr_rule.id)

    def test_rule_rows_are_never_mutated(self):
        before = Rule.objects.get(pk=self.finance.pk).predicates
        services.repoint_rule(self.task.id, {"department": "IT"})
        self.assertEqual(Rule.objects.get(pk=self.finance.pk).predicates, before)

    def test_unstarted_task_is_released_when_the_assignee_stops_qualifying(self):
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.fin_user.id)

        new_rule, _, unassigned = services.repoint_rule(
            self.task.id, {"department": "HR", "max_active_tasks": 5})
        services.materialize_rule(new_rule.id)

        # Released from the Finance user, and their capacity handed back...
        self.assertTrue(unassigned)
        self.fin_user.refresh_from_db()
        self.assertEqual(self.fin_user.active_task_count, 0)
        self.assertEqual(self.fin_user.committed_effort_hours, Decimal("0.00"))

        # ...then materialising the new rule drains the pool onto someone who
        # does qualify. The task does not sit unassigned waiting for a sweep.
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.hr_user.id)

    def test_work_in_progress_is_never_discarded(self):
        """Declared policy: a started task keeps its assignee even if the edit
        makes them ineligible. Discarding work in flight is worse than a
        temporary mismatch."""
        Task.objects.filter(pk=self.task.pk).update(
            status=Task.Status.IN_PROGRESS)

        _, _, unassigned = services.repoint_rule(
            self.task.id, {"department": "HR", "max_active_tasks": 5})

        self.assertFalse(unassigned)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.fin_user.id)


class CacheAndSingleFlightTests(TestCase):
    """Q-3 / Q-4."""

    def setUp(self):
        cache.clear()
        make_user("worker", dept="Finance")
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})

    def test_rule_spec_is_cached(self):
        services.rule_spec(self.rule.id)
        with self.assertNumQueries(0):
            spec = services.rule_spec(self.rule.id)
        self.assertEqual(spec["max_active_tasks"], 5)
        self.assertEqual(spec["predicates"], self.rule.predicates)

    def test_cached_spec_excludes_the_mutable_columns(self):
        """eligible_count and materialized_at change on every materialisation.
        Caching them would serve a stale count to the 'no eligible users'
        branch and misreport why a task is unassigned."""
        spec = services.rule_spec(self.rule.id)
        self.assertEqual(set(spec), {"predicates", "max_active_tasks"})

    def test_single_flight_skips_a_concurrent_materialisation(self):
        cache.add(services.MATERIALIZE_LOCK_KEY.format(self.rule.id), 1,
                  timeout=60)
        self.assertIsNone(services.materialize_rule(self.rule.id))
        self.assertEqual(
            RuleEligibleUser.objects.filter(rule=self.rule).count(), 0)

    def test_lock_is_released_so_the_next_call_proceeds(self):
        self.assertEqual(services.materialize_rule(self.rule.id), 1)
        self.assertEqual(services.materialize_rule(self.rule.id), 1)

    def test_lock_is_released_even_when_materialisation_raises(self):
        with mock.patch.object(services, "_materialize_rule",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                services.materialize_rule(self.rule.id)
        # a wedged lock would make the rule permanently unmaterialisable
        self.assertEqual(services.materialize_rule(self.rule.id), 1)


class AssignmentOutcomeReportingTests(TestCase):
    """Regression: the API claimed a definitive reason for a pending outcome.

    Found only by running the real stack. With a broker, `place_task` is queued
    and has not run when the response is built, so the view reported "N users
    match, all at capacity" for a task the worker assigned a second later. The
    eager test settings hid it entirely.
    """

    def setUp(self):
        self.manager = make_user("mgr", role=User.Role.MANAGER, dept="Operations")
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})
        self.task = Task.objects.create(
            title="t", priority=2, effort_hours=Decimal("1.0"),
            rule=self.rule, created_by=self.manager)

    def test_unresolved_placement_reports_pending_not_a_diagnosis(self):
        Rule.objects.filter(pk=self.rule.pk).update(eligible_count=43)
        self.rule.refresh_from_db()
        outcome = views._assignment_outcome(self.task, self.rule, resolved=False)
        self.assertIn("pending", outcome)
        self.assertNotIn("capacity", outcome)

    def test_resolved_placement_reports_the_structural_cause(self):
        Rule.objects.filter(pk=self.rule.pk).update(eligible_count=0)
        self.rule.refresh_from_db()
        self.assertEqual(
            views._assignment_outcome(self.task, self.rule, resolved=True),
            "unassigned: no user matches this rule")

    def test_resolved_placement_reports_the_transient_cause(self):
        Rule.objects.filter(pk=self.rule.pk).update(eligible_count=43)
        self.rule.refresh_from_db()
        outcome = views._assignment_outcome(self.task, self.rule, resolved=True)
        self.assertIn("43 users match, all at capacity", outcome)

    def test_an_assigned_task_reports_assigned_either_way(self):
        worker = make_user("w", dept="Finance")
        Task.objects.filter(pk=self.task.pk).update(assignee=worker)
        self.task.refresh_from_db()
        for resolved in (True, False):
            self.assertEqual(
                views._assignment_outcome(self.task, self.rule, resolved),
                "assigned")


class NewRuleMaterialisationRaceTests(TestCase):
    """Regression: a task with a brand-new rule pooled and stayed pooled.

    Creation enqueues `materialize_rule` and `place_task` as independent jobs.
    Nothing orders them, so placement routinely ran against an empty
    eligibility table, pooled the task, and nothing retried it but the
    five-minute sweep. Found against the running container stack — the eager
    test settings ran materialisation first every time, so the suite never saw
    it.
    """

    def setUp(self):
        self.manager = make_user("mgr", role=User.Role.MANAGER, dept="Operations")
        make_user("worker", dept="Finance", years=6)

    def test_placing_before_materialisation_still_ends_up_assigned(self):
        rule, _ = services.get_or_create_rule(
            {"department": "Finance", "experience_years": {"gte": 4},
             "max_active_tasks": 5})
        task = Task.objects.create(
            title="new rule", priority=0, effort_hours=Decimal("1.0"),
            rule=rule, created_by=self.manager)

        # the adversarial order: placement first, against an empty table
        self.assertIsNone(services.place_task(task.id))
        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)

        services.materialize_rule(rule.id)

        task.refresh_from_db()
        self.assertIsNotNone(task.assignee_id)

    def test_materialisation_drains_in_priority_order(self):
        rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 1})
        low = Task.objects.create(title="P2", priority=2,
                                  effort_hours=Decimal("1.0"), rule=rule,
                                  created_by=self.manager)
        high = Task.objects.create(title="P0", priority=0,
                                   effort_hours=Decimal("1.0"), rule=rule,
                                   created_by=self.manager)

        services.materialize_rule(rule.id)

        high.refresh_from_db(); low.refresh_from_db()
        self.assertIsNotNone(high.assignee_id)
        self.assertIsNone(low.assignee_id)


class TaskDetailAndPollingTests(TestCase):
    """`placement_attempted_at` is what lets the API stop guessing.

    Without it, an unassigned task is ambiguous between "the worker has not run
    yet" and "the worker ran and found nobody" -- two states needing opposite
    responses, and the source of the false "all at capacity" reply.
    """

    def setUp(self):
        self.manager = make_user("mgr", role=User.Role.MANAGER, dept="Operations")
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})
        self.task = Task.objects.create(
            title="t", priority=1, effort_hours=Decimal("1.0"),
            rule=self.rule, created_by=self.manager)

    def test_before_placement_runs_the_outcome_is_pending(self):
        self.assertIsNone(self.task.placement_attempted_at)
        self.assertIn("pending",
                      views._assignment_outcome(self.task, self.rule))

    def test_placement_stamps_the_task_even_when_it_finds_nobody(self):
        services.place_task(self.task.id)          # no eligible users exist
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.placement_attempted_at)
        self.assertNotIn("pending",
                         views._assignment_outcome(self.task, self.rule))
        self.assertIn("no user matches",
                      views._assignment_outcome(self.task, self.rule))

    def test_the_stamp_is_not_overwritten_on_a_later_attempt(self):
        services.place_task(self.task.id)
        self.task.refresh_from_db()
        first = self.task.placement_attempted_at
        services.place_task(self.task.id)
        self.task.refresh_from_db()
        self.assertEqual(self.task.placement_attempted_at, first)

    def test_detail_endpoint_serves_what_the_ui_polls(self):
        worker = make_user("w", dept="Finance")
        services.materialize_rule(self.rule.id)
        services.place_task(self.task.id)

        # DRF is configured JWT-only, so Django's session login does not
        # authenticate the request -- force_authenticate is the equivalent.
        client = APIClient()
        client.force_authenticate(user=self.manager)
        body = client.get(f"/tasks/{self.task.id}").json()
        self.assertEqual(body["assignee"], worker.username)
        self.assertEqual(body["assignment"], "assigned")
        self.assertEqual(body["rule"], self.rule.predicates)


class TaskLifecycleTests(TestCase):
    """CRUD and the status workflow the brief asks for."""

    def setUp(self):
        self.manager = make_user("mgr", role=User.Role.MANAGER, dept="Operations")
        self.rule, _ = services.get_or_create_rule(
            {"department": "Finance", "max_active_tasks": 5})
        self.worker = make_user("worker", dept="Finance")
        services.materialize_rule(self.rule.id)
        self.task = Task.objects.create(
            title="t", priority=1, effort_hours=Decimal("3.0"),
            rule=self.rule, created_by=self.manager)
        services.place_task(self.task.id)
        self.task.refresh_from_db()

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_todo_to_in_progress_and_back(self):
        services.set_status(self.task.id, Task.Status.IN_PROGRESS)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)
        services.set_status(self.task.id, Task.Status.TODO)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.TODO)

    def test_status_cannot_jump_to_done_through_an_edit(self):
        """Reaching a terminal state moves effort between counters. A PATCH that
        could set status='done' would leave committed_effort_hours overstated
        forever, with nothing to surface it."""
        with self.assertRaises(services.InvalidTransition) as ctx:
            services.set_status(self.task.id, Task.Status.DONE)
        self.assertIn("complete", str(ctx.exception))

    def test_assignee_may_move_their_own_task_but_not_edit_it(self):
        client = self._client(self.worker)
        ok = client.patch(f"/tasks/{self.task.id}",
                          {"status": "in_progress"}, format="json")
        self.assertEqual(ok.status_code, 200)

        denied = client.patch(f"/tasks/{self.task.id}",
                              {"title": "hijacked"}, format="json")
        self.assertEqual(denied.status_code, 403)

    def test_manager_edits_task_fields(self):
        res = self._client(self.manager).patch(
            f"/tasks/{self.task.id}",
            {"title": "renamed", "priority": 0, "effort_hours": "4.5"},
            format="json")
        self.assertEqual(res.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual((self.task.title, self.task.priority), ("renamed", 0))
        self.assertEqual(self.task.effort_hours, Decimal("4.50"))

    def test_deleting_an_assigned_task_hands_capacity_back(self):
        """Otherwise the assignee looks permanently busier than they are and the
        selection ladder is skewed for good."""
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.active_task_count, 1)
        self.assertEqual(self.worker.committed_effort_hours, Decimal("3.00"))

        services.delete_task(self.task.id)

        self.worker.refresh_from_db()
        self.assertEqual(self.worker.active_task_count, 0)
        self.assertEqual(self.worker.committed_effort_hours, Decimal("0.00"))
        self.assertFalse(Task.objects.filter(pk=self.task.id).exists())

    def test_list_is_scoped_by_role(self):
        other = Task.objects.create(
            title="someone else's", priority=2, effort_hours=Decimal("1.0"),
            rule=self.rule, created_by=self.manager)

        seen_by_user = self._client(self.worker).get("/tasks/").json()
        self.assertEqual({t["id"] for t in seen_by_user}, {self.task.id})

        seen_by_manager = self._client(self.manager).get("/tasks/").json()
        self.assertEqual({t["id"] for t in seen_by_manager},
                         {self.task.id, other.id})

    def test_list_filters_the_unassigned_pool(self):
        pooled = Task.objects.create(
            title="pooled", priority=2, effort_hours=Decimal("1.0"),
            rule=self.rule, created_by=self.manager)
        body = self._client(self.manager).get("/tasks/?assigned=false").json()
        self.assertEqual({t["id"] for t in body}, {pooled.id})


class TransitionErrorMessageTests(TestCase):
    """The rejection has to explain itself, not just refuse."""

    def setUp(self):
        self.manager = make_user("mgr", role=User.Role.MANAGER, dept="Operations")
        rule, _ = services.get_or_create_rule({"department": "Finance"})
        self.task = Task.objects.create(
            title="t", priority=1, effort_hours=Decimal("1.0"),
            rule=rule, created_by=self.manager)

    def test_patching_to_done_explains_where_to_go_instead(self):
        client = APIClient()
        client.force_authenticate(user=self.manager)
        res = client.patch(f"/tasks/{self.task.id}",
                           {"status": "done"}, format="json")
        self.assertEqual(res.status_code, 400)
        message = str(res.json()["status"])
        self.assertIn("complete", message)
        self.assertIn("counters", message)
