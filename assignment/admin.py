"""Admin registration, so the schema can be inspected without a psql session.

Read-oriented on purpose. `rule_eligible_user` is derived state and the counters
on `User` are maintained transactionally by the assignment path -- editing
either by hand would desynchronise them from the tasks table with nothing to
surface the drift. Those fields are shown but not editable.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Rule, RuleEligibleUser, Task, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "username", "role", "department", "experience_years", "location",
        "active_task_count", "committed_effort_hours", "lifetime_hours",
    )
    list_filter = ("role", "department", "location")
    search_fields = ("username", "email")
    # The volatile counters are written only inside the assignment and
    # completion transactions (README section 4.1).
    readonly_fields = (
        "active_task_count", "committed_effort_hours", "lifetime_hours",
        "stable_attrs_version",
    )
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Assignment profile", {
            "fields": (
                "role", "department", "experience_years", "location",
                "active_task_count", "committed_effort_hours",
                "lifetime_hours", "stable_attrs_version",
            )
        }),
    )


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("id", "short_fingerprint", "predicates", "max_active_tasks",
                    "eligible_count", "task_count", "materialized_at")
    search_fields = ("fingerprint",)
    # Rules are content-addressed and immutable: editing one would break the
    # guarantee that a fingerprint identifies its predicates (README 1.5).
    readonly_fields = ("fingerprint", "predicates", "max_active_tasks",
                       "eligible_count", "materialized_at")

    @admin.display(description="fingerprint")
    def short_fingerprint(self, obj):
        return obj.fingerprint[:16]

    @admin.display(description="tasks using this rule")
    def task_count(self, obj):
        return obj.tasks.count()


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "priority", "effort_hours", "status",
                    "assignee", "rule_id", "created_at")
    list_filter = ("status", "priority")
    search_fields = ("title",)
    # Assignment is never manual -- that is the premise of the system, so the
    # admin does not offer a back door to it.
    readonly_fields = ("assignee", "placement_attempted_at", "created_at")


@admin.register(RuleEligibleUser)
class RuleEligibleUserAdmin(admin.ModelAdmin):
    """Derived state. Recomputed by `materialize_rule` and `recompute_user`."""

    list_display = ("rule_id", "user")
    list_filter = ("rule_id",)
    search_fields = ("user__username",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
