from __future__ import unicode_literals

import json

from auditlog.diff import model_instance_diff
from auditlog.models import LogEntry


def log_create(sender, instance, created, **kwargs):
    """
    Signal receiver that creates a log entry when a model instance is first saved to the database.

    Direct use is discouraged, connect your model through :py:func:`auditlog.registry.register` instead.
    """
    if created:
        changes = model_instance_diff(None, instance)

        log_entry = LogEntry.objects.log_create(
            instance,
            action=LogEntry.Action.CREATE,
            changes=json.dumps(changes),
        )


OLD_INSTANCE_ATTR = '_auditlog_old_instance'


def store_old_instance(sender, instance, **kwargs):
    """
    Signal receiver that stores the current database state of a model instance before it is saved, so that
    :py:func:`log_update` can compare it with the values that are written.

    Direct use is discouraged, connect your model through :py:func:`auditlog.registry.register` instead.
    """
    old = None
    if instance.pk is not None:
        try:
            old = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            pass
    instance.__dict__[OLD_INSTANCE_ATTR] = old


def log_update(sender, instance, created, **kwargs):
    """
    Signal receiver that creates a log entry when a model instance is changed and saved to the database.

    This runs after the save, because fields may change their value while being saved (for example ``auto_now``
    fields). The state stored by :py:func:`store_old_instance` is compared with the values that were written.

    Direct use is discouraged, connect your model through :py:func:`auditlog.registry.register` instead.
    """
    old = instance.__dict__.pop(OLD_INSTANCE_ATTR, None)
    if created:
        return

    changes = model_instance_diff(old, instance)

    # Log an entry only if there are changes
    if changes:
        log_entry = LogEntry.objects.log_create(
            instance,
            action=LogEntry.Action.UPDATE,
            changes=json.dumps(changes),
        )


def log_delete(sender, instance, **kwargs):
    """
    Signal receiver that creates a log entry when a model instance is deleted from the database.

    Direct use is discouraged, connect your model through :py:func:`auditlog.registry.register` instead.
    """
    if instance.pk is not None:
        changes = model_instance_diff(instance, None)

        log_entry = LogEntry.objects.log_create(
            instance,
            action=LogEntry.Action.DELETE,
            changes=json.dumps(changes),
        )
