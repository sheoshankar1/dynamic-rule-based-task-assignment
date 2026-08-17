import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("assignment")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Both of these are safety nets, not mechanisms. The four triggers in README
# section 6 do the real work; these only catch what infrastructure failure drops,
# which is why they run rarely.
app.conf.beat_schedule = {
    "sweep-unassigned-pool": {
        "task": "assignment.tasks.sweep_unassigned_pool",
        "schedule": 300.0,
    },
    "flag-stuck-tasks": {
        "task": "assignment.tasks.flag_stuck_tasks",
        "schedule": 3600.0,
    },
}
