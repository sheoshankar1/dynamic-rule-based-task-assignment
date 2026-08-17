"""Assignment and eligibility (README sections 5 and 6).

The load-bearing rule of this module: `fill_capacity` is the ONLY place a task
becomes assigned. Every trigger routes through it. A second, direct path lets a
newly created P2 overtake a waiting P0 -- see tests/test_s7_priority_overtake.py,
which reproduces exactly that and then proves this design does not have it.

Raw SQL is used for the two statements that must be exact: the ladder query and
the atomic claim. The ORM cannot express a compare-and-set, and the ladder needs
the index in section 4 to be walked rather than sorted.
"""

import logging
from datetime import timedelta

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from .models import Rule, RuleEligibleUser, Task, User
from .rules import fingerprint, matches, split, to_sql

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# materialisation
# ---------------------------------------------------------------------------


def get_or_create_rule(raw_rule):
    """Content-address a rule. Returns (rule, created).

    If the fingerprint already exists the rule is reused and NO recompute
    happens -- the entire return on content addressing (D1).
    """
    predicates, volatile = split(raw_rule)
    fp = fingerprint(predicates)
    rule, created = Rule.objects.get_or_create(
        fingerprint=fp,
        defaults={
            "predicates": predicates,
            "max_active_tasks": volatile.get("max_active_tasks"),
        },
    )
    # Two rules differing only in their cap share a fingerprint, so the first
    # one to arrive sets it. Recorded as a known simplification: a per-task cap
    # override would need the cap on the task, not the rule.
    return rule, created


RULE_SPEC_KEY = "rule:spec:{}"
MATERIALIZE_LOCK_KEY = "rule:materializing:{}"
MATERIALIZE_LOCK_TTL = 300
# How many pooled tasks a single materialisation will try to place.
PLACE_AFTER_MATERIALIZE = 200
# A rule may omit max_active_tasks, in which case the claim never fails and the
# loop below would drain that rule's whole pool into one user inside a single
# transaction. Capped so the transaction stays short; the sweep picks up any
# remainder on its next run, exactly as it does for a dropped message.
FILL_BATCH = 100


def rule_spec(rule_id):
    """The immutable half of a rule row: predicates and cap.

    Cached without expiry and without invalidation, which is safe *only* for
    these fields. `eligible_count` and `materialized_at` live on the same row
    and DO change every time the rule is materialised -- caching the whole
    model instance would serve a stale count to the "no eligible users" branch
    in section 6 and misreport why a task is unassigned. Splitting the row is
    the point, not an optimisation.
    """
    key = RULE_SPEC_KEY.format(rule_id)
    spec = cache.get(key)
    if spec is None:
        rule = Rule.objects.only("predicates", "max_active_tasks").get(pk=rule_id)
        spec = {
            "predicates": rule.predicates,
            "max_active_tasks": rule.max_active_tasks,
        }
        cache.set(key, spec, timeout=None)
    return spec


def materialize_rule(rule_id):
    """Compute a rule's eligible users from one indexed scan over users.

    Runs in a worker: nobody is waiting on it, so it is deliberately not
    optimised beyond using the composite index.
    """
    # Single-flight (Q-4). Rematerialising a rule is the same work whoever does
    # it, so a loser skips rather than queues: N workers reacting to one
    # invalidation do the scan once, not N times. The lock has a TTL so a worker
    # dying mid-scan cannot wedge the rule permanently.
    lock = MATERIALIZE_LOCK_KEY.format(rule_id)
    if not cache.add(lock, 1, timeout=MATERIALIZE_LOCK_TTL):
        logger.info("materialize_rule(%s) already in flight, skipping", rule_id)
        return None

    try:
        return _materialize_rule(rule_id)
    finally:
        cache.delete(lock)


def _materialize_rule(rule_id):
    rule = Rule.objects.get(pk=rule_id)
    where, params = to_sql(rule.predicates, alias="u")
    # Managers author work and Admins administer the system; neither receives
    # assignments. This is policy, not a rule predicate, so it lives here
    # rather than in the rule engine -- a rule describes which *people* qualify,
    # not which roles the product routes work to.
    where = f"({where}) AND u.role = %s"
    params = [*params, User.Role.USER]

    with transaction.atomic():
        RuleEligibleUser.objects.filter(rule_id=rule_id).delete()
        with connection.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {RuleEligibleUser._meta.db_table} (rule_id, user_id)
                SELECT %s, u.id FROM {User._meta.db_table} u WHERE {where}
                """,
                [rule_id, *params],
            )
            inserted = cur.rowcount
        Rule.objects.filter(pk=rule_id).update(
            eligible_count=inserted, materialized_at=timezone.now()
        )

    # Materialisation is itself an event that can change the answer, and it was
    # missing from the trigger set in README section 6.
    #
    # Task creation enqueues materialize_rule and place_task as INDEPENDENT
    # jobs. Nothing orders them, so for any brand-new rule placement routinely
    # runs first, finds an empty eligibility table, and pools the task -- with
    # nothing to retry it but the 5-minute sweep. Verified against the running
    # stack: 27 eligible users all at zero load, task still unassigned.
    #
    # Draining here closes it. Bounded because a full recompute would otherwise
    # place every pooled task in one job; the sweep covers any remainder.
    pooled = list(
        Task.objects.filter(rule_id=rule_id, assignee__isnull=True)
        .exclude(status__in=Task.TERMINAL)
        .order_by("priority", "created_at")
        .values_list("id", flat=True)[:PLACE_AFTER_MATERIALIZE]
    )
    for task_id in pooled:
        place_task(task_id)

    return inserted


def recompute_user(user_id):
    """One user against every cached rule; write only the delta.

    Not an inverted index: testing one user against ~1k rules is a
    microsecond-scale loop, and an index would be more code, more invalidation
    surface, and slower at this cardinality.
    """
    user = User.objects.get(pk=user_id)
    # Same policy as materialisation: a user who is not a recipient is eligible
    # for nothing, and promoting someone to Manager must remove them from every
    # eligible set rather than leaving stale rows behind.
    should = set()
    if user.role == User.Role.USER:
        should = {r.id for r in Rule.objects.all() if matches(r.predicates, user)}
    current = set(
        RuleEligibleUser.objects.filter(user_id=user_id).values_list(
            "rule_id", flat=True
        )
    )

    added, removed = should - current, current - should
    if removed:
        RuleEligibleUser.objects.filter(
            user_id=user_id, rule_id__in=removed
        ).delete()
    if added:
        RuleEligibleUser.objects.bulk_create(
            [RuleEligibleUser(rule_id=r, user_id=user_id) for r in added]
        )
    for rule_id in added | removed:
        Rule.objects.filter(pk=rule_id).update(
            eligible_count=RuleEligibleUser.objects.filter(rule_id=rule_id).count()
        )

    # Gaining rules can make pooled work placeable -- this is what answers "no
    # eligible user existed when the task was created" (D13).
    if added:
        fill_capacity(user_id)

    return {"added": sorted(added), "removed": sorted(removed)}


# ---------------------------------------------------------------------------
# assignment
# ---------------------------------------------------------------------------

_USERS = User._meta.db_table
_ELIGIBLE = RuleEligibleUser._meta.db_table
_TASKS = Task._meta.db_table

# The four-key ladder. LIMIT 1: the order is total, so there is exactly one
# winner. On a lost race we re-query rather than hold a candidate list -- a
# cached list goes stale as other workers claim, a re-query never does.
_TOP_CANDIDATE = f"""
    SELECT u.id
      FROM {_ELIGIBLE} reu
      JOIN {_USERS} u ON u.id = reu.user_id
     WHERE reu.rule_id = %s
       AND (%s::int IS NULL OR u.active_task_count < %s)
     ORDER BY u.committed_effort_hours ASC,
              u.lifetime_hours        DESC,
              u.date_joined           ASC,
              u.id                    ASC
     LIMIT 1
"""

# Compare-and-set. The cap is evaluated INSIDE the write, so two workers cannot
# both pass a read-then-write check. Postgres locks the row for the duration of
# the UPDATE; the loser blocks, re-reads, re-evaluates, and matches zero rows.
_CLAIM = f"""
    UPDATE {_USERS}
       SET active_task_count      = active_task_count + 1,
           committed_effort_hours = committed_effort_hours + %s
     WHERE id = %s
       AND (%s::int IS NULL OR active_task_count < %s)
    RETURNING id
"""

# The pool, in drain order: P0 first, oldest first within a band.
_NEXT_POOLED_TASK = f"""
    SELECT t.id, t.effort_hours, r.max_active_tasks
      FROM {_TASKS} t
      JOIN {_ELIGIBLE} reu ON reu.rule_id = t.rule_id AND reu.user_id = %s
      JOIN {Rule._meta.db_table} r ON r.id = t.rule_id
     WHERE t.assignee_id IS NULL
       AND t.status NOT IN ('done', 'cancelled')
     ORDER BY t.priority ASC, t.created_at ASC
     LIMIT 1
"""


def top_candidate(rule_id, max_active_tasks):
    with connection.cursor() as cur:
        cur.execute(_TOP_CANDIDATE, [rule_id, max_active_tasks, max_active_tasks])
        row = cur.fetchone()
    return row[0] if row else None


def _claim(cur, user_id, max_active_tasks, effort):
    cur.execute(_CLAIM, [effort, user_id, max_active_tasks, max_active_tasks])
    return cur.fetchone() is not None


@transaction.atomic
def fill_capacity(user_id):
    """The only assignment primitive.

    Drains the pool of tasks this user is eligible for, in priority order, until
    their cap is reached. Because every trigger routes through here, a waiting P0
    is always considered before a newly created P2.

    Returns the list of task ids assigned.
    """
    assigned = []
    with connection.cursor() as cur:
        while len(assigned) < FILL_BATCH:
            cur.execute(_NEXT_POOLED_TASK, [user_id])
            row = cur.fetchone()
            if row is None:
                return assigned

            task_id, effort, cap = row
            if not _claim(cur, user_id, cap, effort):
                return assigned          # at capacity, or lost the race

            cur.execute(
                f"UPDATE {_TASKS} SET assignee_id = %s WHERE id = %s",
                [user_id, task_id],
            )
            assigned.append(task_id)

    if len(assigned) == FILL_BATCH:
        logger.warning(
            "fill_capacity(%s) hit the %s batch cap - an uncapped rule with a "
            "large pool. Remainder left for the sweep.", user_id, FILL_BATCH)
    return assigned


def place_task(task_id):
    """Creation trigger: ask this task's best candidate to fill.

    The candidate then drains THEIR pool in priority order, so the new task is
    taken only if it is the highest-ranked thing that user could have taken.
    Re-queries on a lost race; falls through to the pool when nothing is
    claimable, where the section 5 triggers pick it up.
    """
    task = Task.objects.get(pk=task_id)
    cap = rule_spec(task.rule_id)["max_active_tasks"]
    # Stamped whatever the outcome, so "pooled" stops being ambiguous.
    Task.objects.filter(pk=task_id, placement_attempted_at__isnull=True).update(
        placement_attempted_at=timezone.now()
    )

    while task.assignee_id is None:
        user_id = top_candidate(task.rule_id, cap)
        if user_id is None:
            return None                              # pool it
        fill_capacity(user_id)
        task.refresh_from_db(fields=["assignee_id"])
        if task.assignee_id is not None:
            return task.assignee_id
        # The candidate filled their cap with higher-ranked work. Loop: the next
        # query sees their raised counters and returns someone else. Terminates
        # because each pass consumes at least one slot, and returns None once no
        # candidate remains.
    return task.assignee_id


@transaction.atomic
def complete_task(task_id, cancelled=False):
    """Terminal transition. Frees the slot, moves effort, notifies the author.

    Cancellation frees the slot and decrements committed hours but does NOT
    credit lifetime_hours: no work was delivered, and crediting it would corrupt
    selection key 2 silently and permanently.

"""
    task = Task.objects.select_for_update().get(pk=task_id)
    if task.status in Task.TERMINAL:
        return None
    assignee_id = task.assignee_id

    task.status = Task.Status.CANCELLED if cancelled else Task.Status.DONE
    task.save(update_fields=["status"])

    if assignee_id is not None:
        user = User.objects.select_for_update().get(pk=assignee_id)
        user.active_task_count = max(0, user.active_task_count - 1)
        user.committed_effort_hours -= task.effort_hours
        if not cancelled:
            user.lifetime_hours += task.effort_hours
        user.save(
            update_fields=[
                "active_task_count", "committed_effort_hours", "lifetime_hours"
            ]
        )

    return assignee_id


# ---------------------------------------------------------------------------
# operational: the sweep and the aging flag
# ---------------------------------------------------------------------------

STUCK_AFTER = timedelta(hours=24)


def sweep_unassigned_pool(batch=500):
    """Backstop for a dropped queue message (A-10).

    This is NOT the mechanism -- the four triggers in README section 6 are.
    Polling covers infrastructure failure only, which is why it runs rarely and
    why its batch is bounded. If this sweep is regularly placing tasks, the
    event path is broken and that is the bug to fix.
    """
    pooled = list(
        Task.objects.filter(assignee__isnull=True)
        .exclude(status__in=Task.TERMINAL)
        .order_by("priority", "created_at")
        .values_list("id", flat=True)[:batch]
    )
    placed = [tid for tid in pooled if place_task(tid) is not None]
    if placed:
        logger.warning(
            "sweep placed %s task(s) the event path missed: %s", len(placed), placed
        )
    return placed


def flag_stuck_tasks(older_than=STUCK_AFTER, batch=500):
    """Surface tasks nobody can ever take (A-7).

    A task whose rule matches no user may sit forever, and a task nobody can see
    is indistinguishable from one that was never created. The correct behaviour
    is to make it visible -- never to auto-delete it or quietly relax the rule.

    Emitted as a WARNING to the application log. Notifications were removed from
    scope, and this alert was their only other consumer; logging keeps the
    capability without reintroducing the model. A log line is not as good as a
    push -- it needs someone watching -- so if this ever matters operationally,
    an alert on this logger is the cheapest fix.

    Returns the task ids reported. Not idempotent across runs: the caller
    schedules it, and a repeated warning about a still-stuck task is correct.
    """
    cutoff = timezone.now() - older_than
    stuck = list(
        Task.objects.filter(assignee__isnull=True, created_at__lt=cutoff)
        .exclude(status__in=Task.TERMINAL)
        .order_by("priority", "created_at")
        .values_list("id", "rule_id", "created_by_id")[:batch]
    )
    for task_id, rule_id, author_id in stuck:
        logger.warning(
            "task %s (rule %s, author %s) has been unassignable since before %s",
            task_id, rule_id, author_id, cutoff.isoformat(),
        )
    return [t[0] for t in stuck]


# ---------------------------------------------------------------------------
# rule edits (Story 4)
# ---------------------------------------------------------------------------


def repoint_rule(task_id, raw_rule):
    """Change a task's rule (E-7). Rules are immutable: this swaps a foreign
    key, it never mutates a rule row.

    Returns (rule, created, unassigned). If the fingerprint already exists there
    is NO recompute -- the usual case, and the whole return on content
    addressing (D1/D4).

    Declared policy on an assignee who no longer qualifies: a task still in
    `todo` is unassigned and re-placed, because the author has just redefined
    who may do it and nothing has been started. A task already `in_progress` is
    left alone and reported -- discarding work in flight is worse than a
    temporary mismatch, and the same reasoning applies to effort edits
    (README section 11).
    """
    rule, created = get_or_create_rule(raw_rule)
    unassigned = False

    with transaction.atomic():
        task = Task.objects.select_for_update().select_related("rule").get(pk=task_id)
        if task.rule_id == rule.id:
            return rule, created, False

        task.rule = rule
        task.save(update_fields=["rule"])

        if task.assignee_id is not None and task.status == Task.Status.TODO:
            still_eligible = RuleEligibleUser.objects.filter(
                rule_id=rule.id, user_id=task.assignee_id
            ).exists()
            if not still_eligible:
                _release(task)
                unassigned = True

    return rule, created, unassigned


def _release(task):
    """Return an unstarted task to the pool and give its slot back."""
    user = User.objects.select_for_update().get(pk=task.assignee_id)
    user.active_task_count = max(0, user.active_task_count - 1)
    user.committed_effort_hours -= task.effort_hours
    user.save(update_fields=["active_task_count", "committed_effort_hours"])
    Task.objects.filter(pk=task.pk).update(assignee=None)


# ---------------------------------------------------------------------------
# task lifecycle
# ---------------------------------------------------------------------------

# todo <-> in_progress is a working state the assignee owns. Reaching a terminal
# state is NOT here: `done` and `cancelled` move effort between the denormalised
# counters, so they go through complete_task() and cannot be reached by editing
# a status field. A PATCH that could set status='done' directly would silently
# leave committed_effort_hours overstated forever.
OPEN_TRANSITIONS = {
    Task.Status.TODO: {Task.Status.IN_PROGRESS},
    Task.Status.IN_PROGRESS: {Task.Status.TODO},
}


class InvalidTransition(ValueError):
    pass


@transaction.atomic
def set_status(task_id, new_status):
    task = Task.objects.select_for_update().get(pk=task_id)
    allowed = OPEN_TRANSITIONS.get(task.status, set())
    if new_status not in allowed:
        raise InvalidTransition(
            f"cannot move {task.status} -> {new_status}. "
            f"Allowed from here: {sorted(allowed) or 'none'}. "
            f"Use /tasks/{task_id}/complete for done or cancelled, so the "
            f"capacity counters are updated in the same transaction."
        )
    task.status = new_status
    task.save(update_fields=["status"])
    return task


@transaction.atomic
def delete_task(task_id):
    """Deleting an assigned task must hand its capacity back.

    Otherwise the assignee's committed_effort_hours and active_task_count stay
    inflated forever, and every later selection treats them as busier than they
    are -- a silent, permanent skew of the ladder.
    """
    task = Task.objects.select_for_update().get(pk=task_id)
    if task.assignee_id is not None and task.status not in Task.TERMINAL:
        _release(task)
    task.delete()


@transaction.atomic
def update_task_fields(task_id, changes):
    """Edit a task's own fields, keeping the capacity counters truthful.

    `effort_hours` is not an ordinary field: while a task is assigned and open,
    its effort is part of the assignee's `committed_effort_hours`. Writing the
    new value without moving that difference leaves the counter permanently
    wrong -- the assignee looks busier or idler than they are, every later
    selection is skewed, and nothing surfaces it.

    Verified before the fix: editing a 0.50h task to 99.00h left the assignee's
    committed hours at 17.50 while the true sum was 116.00.
    """
    task = Task.objects.select_for_update().get(pk=task_id)
    delta = None
    if "effort_hours" in changes:
        delta = changes["effort_hours"] - task.effort_hours

    for field, value in changes.items():
        setattr(task, field, value)
    task.save(update_fields=list(changes))

    # Only open, assigned work is counted in committed hours.
    if delta and task.assignee_id and task.status not in Task.TERMINAL:
        User.objects.filter(pk=task.assignee_id).update(
            committed_effort_hours=F("committed_effort_hours") + delta
        )
    return task
