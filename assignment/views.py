"""API surface (README section 8).

Views stay thin: they validate, delegate to services, and serialise. The
assignment logic is not reachable from here except through `place_task`.
"""

from decimal import Decimal

from django.conf import settings
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from . import services, tasks
from .models import Rule, RuleEligibleUser, Task, User
from .rules import InvalidRule


class TaskCreateSerializer(serializers.ModelSerializer):
    rules = serializers.DictField(write_only=True)

    class Meta:
        model = Task
        fields = (
            "id", "title", "description", "due_date",
            "priority", "effort_hours", "rules",
        )

    def validate_effort_hours(self, value):
        if value <= 0:
            raise serializers.ValidationError("effort_hours must be positive")
        return value


class TaskListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    priority = serializers.IntegerField()
    effort_hours = serializers.CharField()
    status = serializers.CharField()
    due_date = serializers.DateField(allow_null=True)
    assignee = serializers.CharField(allow_null=True)


class TaskEditSerializer(serializers.Serializer):
    """Task fields a Manager may edit. `rules` is handled separately because it
    repoints a foreign key; `status` is validated against the transition map."""
    title = serializers.CharField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    priority = serializers.IntegerField(required=False, min_value=0)
    effort_hours = serializers.DecimalField(
        required=False, max_digits=6, decimal_places=2, min_value=Decimal("0.01"))
    # Every status is accepted here on purpose. Which transitions are legal
    # lives in services.OPEN_TRANSITIONS and nowhere else -- restricting the
    # choices here too would duplicate the rule and, worse, replace an
    # explanation ("use /complete so the counters move") with '"done" is not a
    # valid choice.'
    status = serializers.ChoiceField(
        required=False, choices=Task.Status.choices)


class TaskCreateResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    rule_fingerprint = serializers.CharField()
    rule_reused = serializers.BooleanField(
        help_text="True when the rule already existed: no recompute was needed")
    assignee = serializers.IntegerField(allow_null=True)
    assignment = serializers.CharField(
        help_text="assigned, or why not: no user matches vs all at capacity")


class EligibleUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    committed_effort_hours = serializers.CharField(help_text="selection key 1, asc")
    lifetime_hours = serializers.CharField(help_text="selection key 2, desc")
    active_task_count = serializers.IntegerField()


class EligibleUsersResponseSerializer(serializers.Serializer):
    task = serializers.IntegerField()
    eligible_total = serializers.IntegerField(
        allow_null=True, help_text="size of the materialised set, before the cap filter")
    with_capacity = EligibleUserSerializer(many=True)


class MyTaskSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    priority = serializers.IntegerField(help_text="0 = P0, highest")
    effort_hours = serializers.CharField()
    status = serializers.CharField()
    due_date = serializers.DateField(allow_null=True)


class TaskUpdateResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    rule_fingerprint = serializers.CharField()
    rule_reused = serializers.BooleanField()
    recompute_required = serializers.BooleanField()
    unassigned_on_edit = serializers.BooleanField(
        help_text="True when an unstarted task was released because its "
                  "assignee no longer satisfies the new rule")
    assignee = serializers.IntegerField(allow_null=True)


class CompleteResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()


class RecomputeResponseSerializer(serializers.Serializer):
    accepted = serializers.IntegerField()
    unknown_rule_ids = serializers.ListField(child=serializers.IntegerField())
    job_ids = serializers.ListField(child=serializers.CharField())
    detail = serializers.CharField()


class RuleUpdateSerializer(serializers.Serializer):
    rules = serializers.DictField()


@extend_schema_view(
    get=extend_schema(
        responses={200: TaskListItemSerializer(many=True)},
        parameters=[
            OpenApiParameter("status", str),
            OpenApiParameter("priority", int),
            OpenApiParameter("assigned", bool,
                             description="true = assigned only, false = the pool"),
            OpenApiParameter("limit", int, description="max 200"),
            OpenApiParameter("offset", int),
        ],
        summary="List tasks",
        description="Users see their own work; Managers and Admins see all.",
    ),
)
@extend_schema(
    methods=["POST"],
    request=TaskCreateSerializer,
    responses={201: TaskCreateResponseSerializer},
    summary="Create a task with rules",
    description="The task is not assigned here. It enters the pool and its "
                "rule's best candidate is asked to fill, so a waiting P0 always "
                "wins over this new task.",
)
class TaskCreateView(APIView):
    """POST /tasks/ -- create a task with rules.

    The task is NOT assigned here. It enters the pool and its rule's best
    candidate is asked to fill; that candidate drains their own pool in priority
    order, so a waiting P0 always wins over this new task.
    """

    def get(self, request):
        qs = Task.objects.select_related("assignee").order_by("priority", "created_at")
        # A User has no business browsing the whole board; visibility follows
        # assignment, exactly as it does for /my-eligible-tasks.
        if request.user.role == User.Role.USER:
            qs = qs.filter(assignee=request.user)

        if (st := request.query_params.get("status")):
            qs = qs.filter(status=st)
        if (pr := request.query_params.get("priority")):
            qs = qs.filter(priority=pr)
        assigned = request.query_params.get("assigned")
        if assigned is not None:
            qs = qs.filter(assignee__isnull=str(assigned).lower() in ("0", "false", "no"))

        offset = max(int(request.query_params.get("offset", 0)), 0)
        limit = min(int(request.query_params.get("limit", 50)), 200)
        return Response([
            {
                "id": t.id, "title": t.title, "priority": t.priority,
                "effort_hours": str(t.effort_hours), "status": t.status,
                "due_date": t.due_date,
                "assignee": t.assignee.username if t.assignee else None,
            }
            for t in qs[offset:offset + limit]
        ])

    def post(self, request):
        if request.user.role not in (User.Role.MANAGER, User.Role.ADMIN):
            return Response(
                {"detail": "only Managers and Admins may author tasks"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = TaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_rule = serializer.validated_data.pop("rules")

        try:
            rule, created = services.get_or_create_rule(raw_rule)
        except InvalidRule as exc:
            return Response({"rules": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            task = Task.objects.create(
                rule=rule, created_by=request.user, **serializer.validated_data
            )

        # Exactly one job, never two. Materialisation drains its own rule's pool
        # (README section 7.2), so for a new fingerprint `place_task` would be a
        # second enqueue doing work the first job already does. Each enqueue is
        # a broker round trip on the request path, and this endpoint has the
        # tightest latency budget in the system.
        if created or rule.materialized_at is None:
            tasks.materialize_rule.delay(rule.id)      # materialises, then places
        else:
            tasks.place_task.delay(task.id)            # already warm, just place

        # With a real broker, placement has not run when this response is built,
        # so re-reading the row cannot show an assignee -- it is a round trip
        # that always returns what we already have. Only worth it when jobs ran
        # inline and the row really did change.
        if settings.CELERY_TASK_ALWAYS_EAGER:
            task.refresh_from_db()
        return Response(
            {
                "id": task.id,
                "rule_fingerprint": rule.fingerprint,
                "rule_reused": not created,
                "assignee": task.assignee_id,
                "assignment": _assignment_outcome(
                    task, rule, settings.CELERY_TASK_ALWAYS_EAGER),
            },
            status=status.HTTP_201_CREATED,
        )


def _assignment_outcome(task, rule, resolved=None):
    """Why this task is or is not assigned.

    `resolved` says whether placement has actually run. With a real broker it
    has NOT by the time this response is built -- `place_task` is queued and a
    worker picks it up milliseconds later. Reporting "all at capacity" then is
    simply false, and it was: verified against the running stack, a task that
    the worker assigned one second later came back claiming 43 users were all
    at capacity.

    The eager test settings hid this completely, because placement finishes
    inline before the response is serialised. Both branches now exist and the
    unit test pins them.
    """
    if task.assignee_id is not None:
        return "assigned"
    # `resolved` is the create-response's own knowledge (it just enqueued the
    # job). On any later read the task itself carries the fact.
    if resolved is None:
        resolved = task.placement_attempted_at is not None
    if not resolved:
        return "pending: placement queued, poll the task for the outcome"
    eligible = rule.eligible_count or 0
    if eligible == 0:
        return "unassigned: no user matches this rule"
    return f"unassigned: {eligible} users match, all at capacity"


@extend_schema(
    responses={200: EligibleUsersResponseSerializer},
    parameters=[OpenApiParameter("limit", int, description="max 200")],
    summary="Eligible users for a task, in selection-ladder order",
)
class EligibleUsersView(APIView):
    """GET /tasks/{id}/eligible-users -- cached stable set, live cap filter,
    in ladder order."""

    def get(self, request, pk):
        # Eligibility exposes colleagues' usernames and current workload, so it
        # is an authoring view, not something every recipient may enumerate.
        if request.user.role not in (User.Role.MANAGER, User.Role.ADMIN):
            return Response({"detail": "only Managers and Admins may inspect eligibility"},
                            status=status.HTTP_403_FORBIDDEN)
        task = get_object_or_404(Task.objects.select_related("rule"), pk=pk)
        cap = task.rule.max_active_tasks
        qs = (
            User.objects.filter(eligible_for__rule_id=task.rule_id)
            .order_by("committed_effort_hours", "-lifetime_hours", "date_joined", "id")
        )
        if cap is not None:
            qs = qs.filter(active_task_count__lt=cap)
        limit = min(int(request.query_params.get("limit", 50)), 200)
        return Response({
            "task": task.id,
            "eligible_total": task.rule.eligible_count,
            "with_capacity": [
                {
                    "id": u.id,
                    "username": u.username,
                    "committed_effort_hours": str(u.committed_effort_hours),
                    "lifetime_hours": str(u.lifetime_hours),
                    "active_task_count": u.active_task_count,
                }
                for u in qs[:limit]
            ],
        })


@extend_schema(
    responses={200: MyTaskSerializer(many=True)},
    summary="Tasks assigned to the caller",
    description="The conjunction of eligible AND assigned. Assignment "
                "implies eligibility, so this is a bounded index lookup.",
)
class MyEligibleTasksView(APIView):
    """GET /my-eligible-tasks -- the conjunction: tasks assigned to the caller.

    Assignment already implies eligibility, so this is a bounded index lookup
    and does not touch rule_eligible_user at all.
    """

    def get(self, request):
        qs = (
            Task.objects.filter(assignee=request.user)
            .exclude(status__in=Task.TERMINAL)
            .order_by("priority", "due_date")
        )
        return Response([
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "priority": t.priority,
                "effort_hours": str(t.effort_hours),
                "status": t.status,
                "due_date": t.due_date,
            }
            for t in qs
        ])


@extend_schema(
    request=None,
    responses={200: CompleteResponseSerializer},
    parameters=[OpenApiParameter(
        "cancelled", bool,
        description="Frees the slot without crediting lifetime_hours")],
    summary="Complete or cancel a task",
)
class CompleteTaskView(APIView):
    """POST /tasks/{id}/complete -- frees the slot and notifies the author.

    ?cancelled=1 frees the slot without crediting lifetime_hours.
    """

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if task.assignee_id != request.user.id and request.user.role == User.Role.USER:
            return Response(
                {"detail": "not your task"}, status=status.HTTP_403_FORBIDDEN
            )

        cancelled = str(request.query_params.get("cancelled", "")).lower() in (
            "1", "true", "yes",
        )
        assignee_id = services.complete_task(task.id, cancelled=cancelled)
        if assignee_id is None and task.assignee_id is None:
            return Response({"detail": "task was not assigned"}, status=200)

        # Capacity just freed -- drain the pool for that user.
        if assignee_id is not None:
            tasks.fill_capacity.delay(assignee_id)
        return Response({"id": task.id, "status": Task.objects.get(pk=pk).status})


@extend_schema(
    request=RuleUpdateSerializer,
    responses={200: TaskUpdateResponseSerializer},
    summary="Change a task's rules",
    description="Rules are immutable; this repoints a foreign key.",
)
class TaskUpdateView(APIView):
    """PATCH /tasks/{id} -- change a task's rules (Story 4).

    Rules are immutable, so this repoints a foreign key. When the fingerprint
    already exists there is no recompute at all, which is the usual case and the
    whole return on content addressing.
    """

    def patch(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        is_author = request.user.role in (User.Role.MANAGER, User.Role.ADMIN)

        # An assignee owns their working state (todo <-> in_progress) without
        # being able to edit the task itself.
        edits = {k: v for k, v in request.data.items() if k != "rules"}
        if edits:
            if not is_author and set(edits) != {"status"}:
                return Response(
                    {"detail": "only Managers and Admins may edit task fields"},
                    status=status.HTTP_403_FORBIDDEN)
            if not is_author and task.assignee_id != request.user.id:
                return Response({"detail": "not your task"},
                                status=status.HTTP_403_FORBIDDEN)

            edit = TaskEditSerializer(data=edits, partial=True)
            edit.is_valid(raise_exception=True)
            changed = edit.validated_data
            new_status = changed.pop("status", None)
            if changed:
                services.update_task_fields(pk, changed)
            if new_status:
                try:
                    services.set_status(pk, new_status)
                except services.InvalidTransition as exc:
                    return Response({"status": str(exc)},
                                    status=status.HTTP_400_BAD_REQUEST)

        raw_rule = request.data.get("rules")
        if not raw_rule:
            task.refresh_from_db()
            return Response({
                "id": task.id,
                "rule_fingerprint": task.rule.fingerprint,
                "rule_reused": True,
                "recompute_required": False,
                "unassigned_on_edit": False,
                "assignee": task.assignee_id,
            })

        if not is_author:
            return Response({"detail": "only Managers and Admins may edit rules"},
                            status=status.HTTP_403_FORBIDDEN)
        try:
            rule, created, unassigned = services.repoint_rule(pk, raw_rule)
        except InvalidRule as exc:
            return Response({"rules": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if created or rule.materialized_at is None:
            tasks.materialize_rule.delay(rule.id)
        task = Task.objects.get(pk=pk)
        if task.assignee_id is None:
            tasks.place_task.delay(pk)

        task.refresh_from_db()
        return Response({
            "id": task.id,
            "rule_fingerprint": rule.fingerprint,
            "rule_reused": not created,
            "recompute_required": created,
            "unassigned_on_edit": unassigned,
            "assignee": task.assignee_id,
        })


@extend_schema(
    request=None,
    responses={202: RecomputeResponseSerializer},
    summary="Queue a full or partial eligibility recompute",
)
class RecomputeEligibilityView(APIView):
    """POST /tasks/recompute-eligibility -- admin escape hatch (D17, Q-5).

    Returns 202 with a job id. A full recompute at the stated scale is minutes
    of worker time; a synchronous endpoint would either time out or lie about
    what it costs. This is an operational tool for after a bad migration, not a
    request-path operation.

    Idempotent per rule: a rule already being materialised is skipped by the
    single-flight lock in `services.materialize_rule`, so submitting twice does
    the work once.
    """

    def post(self, request):
        if request.user.role != User.Role.ADMIN:
            return Response({"detail": "admin only"},
                            status=status.HTTP_403_FORBIDDEN)

        rule_ids = request.data.get("rule_ids")
        if rule_ids is None:
            rule_ids = list(Rule.objects.values_list("id", flat=True))
        elif not isinstance(rule_ids, list):
            return Response({"rule_ids": "must be a list, or omitted for all"},
                            status=status.HTTP_400_BAD_REQUEST)

        known = set(Rule.objects.filter(id__in=rule_ids)
                    .values_list("id", flat=True))
        unknown = sorted(set(rule_ids) - known)

        job_ids = [tasks.materialize_rule.delay(rid).id for rid in sorted(known)]
        return Response(
            {
                "accepted": len(known),
                "unknown_rule_ids": unknown,
                "job_ids": job_ids,
                "detail": "queued; rules already materialising are skipped by "
                          "the single-flight lock",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    # Read-only, and forced to USER on create. Signup is an unauthenticated
    # endpoint: accepting a role from the request body let anyone register as
    # an admin and then call the admin-only endpoints. Role changes are an
    # administrative action, not something the registrant chooses.
    role = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "username", "password", "role", "department",
                  "experience_years", "location")

    def create(self, validated):
        validated.pop("role", None)          # belt and braces if fields change
        # create_user hashes; User.objects.create would store it in clear.
        return User.objects.create_user(role=User.Role.USER, **validated)


@extend_schema(
    request=SignupSerializer,
    responses={201: SignupSerializer},
    summary="Register a user",
)
class SignupView(APIView):
    """POST /auth/signup -- open registration.

    Creating a user fires the stable-attribute signal, so a new account is
    evaluated against every existing rule and may immediately pick up pooled
    work. That is the "user created" trigger from README section 6, not a
    special case for signup.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(SignupSerializer(user).data,
                        status=status.HTTP_201_CREATED)


class TaskDetailSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    due_date = serializers.DateField(allow_null=True)
    priority = serializers.IntegerField()
    effort_hours = serializers.CharField()
    status = serializers.CharField()
    rule = serializers.DictField()
    rule_fingerprint = serializers.CharField()
    created_by = serializers.CharField()
    assignee = serializers.CharField(allow_null=True)
    assignment = serializers.CharField()


@extend_schema_view(
    patch=extend_schema(
        request=RuleUpdateSerializer,
        responses={200: TaskUpdateResponseSerializer},
        summary="Change a task's rules",
    ),
)
@extend_schema(
    methods=["GET"],
    responses={200: TaskDetailSerializer},
    summary="Task detail, including the current assignment outcome",
    description="Assignment happens in a worker, so a task created a moment "
                "ago may still report `pending`. Poll this until `assignee` "
                "is set or the outcome stops being pending.",
)
class TaskDetailView(TaskUpdateView):
    """GET and PATCH share /tasks/{id}. PATCH is inherited rather than copied,
    so there is one implementation of the rule-repoint path."""

    def get(self, request, pk):
        task = get_object_or_404(
            Task.objects.select_related("rule", "assignee", "created_by"), pk=pk)
        # Visibility follows assignment, exactly as it does for the list and for
        # /my-eligible-tasks. A recipient has no business reading the board.
        if (request.user.role == User.Role.USER
                and task.assignee_id != request.user.id):
            return Response({"detail": "not your task"},
                            status=status.HTTP_403_FORBIDDEN)
        return Response({
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "due_date": task.due_date,
            "priority": task.priority,
            "effort_hours": str(task.effort_hours),
            "status": task.status,
            "rule": task.rule.predicates,
            "rule_fingerprint": task.rule.fingerprint,
            "created_by": task.created_by.username,
            "assignee": task.assignee.username if task.assignee else None,
            "assignment": _assignment_outcome(task, task.rule),
        })

    @extend_schema(responses={204: None}, summary="Delete a task")
    def delete(self, request, pk):
        if request.user.role not in (User.Role.MANAGER, User.Role.ADMIN):
            return Response({"detail": "only Managers and Admins may delete tasks"},
                            status=status.HTTP_403_FORBIDDEN)
        get_object_or_404(Task, pk=pk)
        services.delete_task(pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RoleTokenSerializer(TokenObtainPairSerializer):
    """Adds `role` and `username` to the access token.

    The UI uses this to hide actions the caller cannot perform. It is not
    authorisation -- every endpoint still checks the role server-side. A client
    that forged the claim would simply see a button and then a 403.
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["username"] = user.username
        return token


@extend_schema(summary="Obtain a JWT pair; the access token carries role")
class RoleTokenObtainPairView(TokenObtainPairView):
    serializer_class = RoleTokenSerializer


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


@extend_schema(
    request=LogoutSerializer,
    responses={205: None},
    summary="Sign out; blacklists the refresh token",
    description="Access tokens are stateless and remain valid until they "
                "expire, which is why they are short-lived. Blacklisting the "
                "refresh token stops the session being extended.",
)
class LogoutView(APIView):
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            # Already blacklisted, or malformed. Signing out twice is not an
            # error worth reporting to someone who is trying to leave.
            pass
        return Response(status=status.HTTP_205_RESET_CONTENT)
