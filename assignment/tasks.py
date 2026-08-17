"""Celery jobs. Thin wrappers -- the logic lives in services so it is testable
without a broker."""

from celery import shared_task

from . import services


@shared_task
def materialize_rule(rule_id):
    return services.materialize_rule(rule_id)


@shared_task
def recompute_user(user_id):
    return services.recompute_user(user_id)


@shared_task
def fill_capacity(user_id):
    return services.fill_capacity(user_id)


@shared_task
def place_task(task_id):
    return services.place_task(task_id)


@shared_task
def sweep_unassigned_pool():
    """Backstop only. If this places tasks regularly, the event path is broken."""
    return services.sweep_unassigned_pool()


@shared_task
def flag_stuck_tasks():
    return services.flag_stuck_tasks()
