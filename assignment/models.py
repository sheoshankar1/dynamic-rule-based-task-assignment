"""Schema for README section 2.

The stable/volatile split is the organising idea: stable fields feed the rule
fingerprint and the materialised eligibility table; volatile fields are read at
query time and never materialised.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"          # system administration
        MANAGER = "manager", "Manager"    # authors rules and tasks
        USER = "user", "User"             # receives tasks

    class Department(models.TextChoices):
        FINANCE = "Finance", "Finance"
        HR = "HR", "HR"
        IT = "IT", "IT"
        OPERATIONS = "Operations", "Operations"

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.USER)

    # --- stable: fingerprinted, materialised --------------------------------
    department = models.CharField(
        max_length=16, choices=Department.choices, default=Department.OPERATIONS
    )
    experience_years = models.PositiveSmallIntegerField(default=0)
    location = models.CharField(max_length=64, blank=True, default="")

    # --- volatile: never materialised ---------------------------------------
    # the capacity cap is checked against this
    active_task_count = models.PositiveIntegerField(default=0)
    # selection key 1 (ASC): current burden, in hours so task sizes compare
    committed_effort_hours = models.DecimalField(
        max_digits=7, decimal_places=2, default=0
    )
    # selection key 2 (DESC): track record
    lifetime_hours = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    # selection key 3 (ASC) is AbstractUser.date_joined; key 4 is id

    stable_attrs_version = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            # materialisation: one rule -> its eligible users
            models.Index(fields=["department", "experience_years"]),
            # user selection, in ladder order. Postgres walks this and stops at
            # LIMIT 1 instead of sorting the whole eligible set (README section 6).
            models.Index(
                fields=["committed_effort_hours", "-lifetime_hours", "date_joined", "id"],
                name="users_selection_order",
            ),
        ]

    # `role` is stable in the same sense as the others -- it changes on an HR
    # event, not on every assignment -- so it is materialised, and a change to
    # it recomputes eligibility like any other stable attribute.
    STABLE_FIELDS = ("department", "experience_years", "location", "role")

    def stable_snapshot(self):
        return {f: getattr(self, f) for f in self.STABLE_FIELDS}

    @classmethod
    def from_db(cls, db, field_names, values):
        """Remember the stable fields as loaded, so post_save can tell whether
        they actually changed without issuing a second query."""
        instance = super().from_db(db, field_names, values)
        if set(cls.STABLE_FIELDS).issubset(field_names):
            instance._stable_loaded = instance.stable_snapshot()
        return instance

    def stable_fields_changed(self):
        """True for a new row, or when a stable field differs from what was
        loaded. Rows written by .update() or bulk_create never pass through
        here -- see assignment/signals.py."""
        loaded = getattr(self, "_stable_loaded", None)
        return loaded is None or loaded != self.stable_snapshot()


class Rule(models.Model):
    """Immutable and content-addressed. Editing a task's rule repoints its FK;
    it never mutates a row here."""

    fingerprint = models.CharField(max_length=64, unique=True)
    predicates = models.JSONField()                       # stable predicates only
    max_active_tasks = models.PositiveIntegerField(null=True, blank=True)
    eligible_count = models.PositiveIntegerField(null=True, blank=True)
    materialized_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Rule({self.fingerprint[:12]} {self.predicates})"


class RuleEligibleUser(models.Model):
    """Stable eligibility only.

    Deviation from README section 2, declared: the design specifies a composite
    primary key (rule_id, user_id). Django 4.2 cannot express one, so this
    carries an implicit BigAutoField plus a unique constraint. Cost is roughly
    30 bytes/row over the design -- about 150 MB at 5M rows. It shifts no
    conclusion in section 10, whose sensitivity table is order-of-magnitude.
    Django 5.2's CompositePrimaryKey removes the deviation.
    """

    # db_index=False on both: Django would add a single-column index per FK, and
    # both are redundant here -- `uniq_rule_user` (rule, user) already serves
    # rule_id lookups by prefix, and the (user, rule) index serves user_id.
    # Measured at 15.4M rows: the two redundant indexes cost 233 MB.
    rule = models.ForeignKey(
        Rule, on_delete=models.CASCADE, related_name="eligible", db_index=False
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="eligible_for", db_index=False
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rule", "user"], name="uniq_rule_user")
        ]
        indexes = [models.Index(fields=["user", "rule"])]   # the reverse direction


class Task(models.Model):
    class Status(models.TextChoices):
        TODO = "todo", "Todo"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL = (Status.DONE, Status.CANCELLED)

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    due_date = models.DateField(null=True, blank=True)

    # 0 = P0 (highest). smallint, not text: 'P10' sorts before 'P2' lexically.
    priority = models.PositiveSmallIntegerField(default=2)
    # numeric, not float: this feeds a running sum, and float drift would
    # silently corrupt selection key 1.
    effort_hours = models.DecimalField(max_digits=6, decimal_places=2)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.TODO
    )
    rule = models.ForeignKey(Rule, on_delete=models.PROTECT, related_name="tasks")
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="authored_tasks"
    )
    assignee = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_tasks",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Set the first time placement actually runs for this task. Without it,
    # an unassigned task is ambiguous between "the worker has not got to it
    # yet" and "the worker ran and found nobody" -- two states that need
    # completely different responses, and which the API previously guessed at.
    placement_attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # the unassigned pool, in drain order. ASC on priority: 0 = P0 first.
            models.Index(
                fields=["rule", "priority", "created_at"],
                name="tasks_pool_drain",
                condition=models.Q(assignee__isnull=True) & ~models.Q(
                    status__in=["done", "cancelled"]
                ),
            ),
            # a user's own work, in the order /my-eligible-tasks returns it
            models.Index(
                fields=["assignee", "priority", "due_date"],
                name="tasks_my_work",
                condition=~models.Q(status__in=["done", "cancelled"]),
            ),
        ]

    def __str__(self):
        return f"Task({self.id} P{self.priority} {self.title!r})"
