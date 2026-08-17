"""Wiring for Story 3: a user's stable attributes change, eligibility follows.

Two properties matter and both are easy to get wrong:

1. ONLY stable fields trigger work. `active_task_count`, `committed_effort_hours`
   and `lifetime_hours` change on every assignment and completion; if they
   triggered a recompute the system would spend its life recomputing itself.
   That is the entire point of the stable/volatile split (README D2).

2. A burst of edits produces ONE recompute, scheduled after the burst settles.

Known limitation, declared: `queryset.update()` and `bulk_create()` do not send
signals. Any code path that writes stable fields that way -- the seed command
does -- must call `services.recompute_user` itself. That is Django's contract,
not something this module can paper over.
"""

import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import User

logger = logging.getLogger(__name__)

# How long to absorb a burst of edits to the same user. The recompute is
# scheduled this far out AND the key blocks re-enqueues for the same window, so
# the job reads state after the burst rather than partway through it.
DEBOUNCE_SECONDS = 2


def schedule_recompute(user_id, delay=DEBOUNCE_SECONDS):
    """Enqueue at most one recompute per user per window.

    `cache.add` is a no-op when the key already exists (SETNX on the Redis
    backend), so only the first edit in a window wins. Later edits inside the
    window are not lost -- the job runs after the window closes and reads
    current state, not the state that triggered it.
    """
    if not cache.add(f"recompute:user:{user_id}", 1, timeout=delay):
        logger.debug("recompute for user %s already scheduled", user_id)
        return False

    transaction.on_commit(
        lambda: tasks_module().recompute_user.apply_async(
            (user_id,), countdown=delay
        )
    )
    return True


@receiver(post_save, sender=User, dispatch_uid="assignment.user_stable_attrs")
def user_saved(sender, instance, created, update_fields=None, **kwargs):
    # Fast path: a save naming its fields and touching no stable one cannot have
    # changed eligibility. Every assignment and completion takes this path, so
    # it is the common case by a wide margin.
    if update_fields is not None and not set(update_fields) & set(User.STABLE_FIELDS):
        return

    if not created and not instance.stable_fields_changed():
        return

    # .update() does not re-fire post_save, so this cannot recurse.
    User.objects.filter(pk=instance.pk).update(
        stable_attrs_version=F("stable_attrs_version") + 1
    )
    schedule_recompute(instance.pk)


def tasks_module():
    """Indirection so tests can patch the Celery job without importing it at
    module load time (which would create a circular import)."""
    from . import tasks

    return tasks
