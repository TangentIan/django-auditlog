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


def log_update(sender, instance, update_fields=None, **kwargs):
    """
    Signal receiver that creates a log entry when a model instance is changed and saved to the database.

    When the save is limited by ``update_fields``, only changes to those fields are logged, as other fields are not
    written to the database.

    Direct use is discouraged, connect your model through :py:func:`auditlog.registry.register` instead.
    """
    if instance.pk is not None:
        try:
            old = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            pass
        else:
            new = instance

            changes = model_instance_diff(old, new)

            if changes and update_fields is not None:
                saved_fields = set(
                    field.name for field in sender._meta.concrete_fields
                    if field.name in update_fields or field.attname in update_fields
                )
                changes = dict(
                    (name, change) for name, change in changes.items() if name in saved_fields
                )

            # Log an entry only if there are changes
            if changes:
                log_entry = LogEntry.objects.log_create(
                    instance,
                    action=LogEntry.Action.UPDATE,
                    changes=json.dumps(changes),
                )


DELETE_SNAPSHOT_ATTR = '_auditlog_delete_snapshot'


def snapshot_delete(sender, instance, using=None, **kwargs):
    """
    Signal receiver that captures the persisted state of a model instance right before it is deleted from the database.

    The row is reloaded from the database the deletion runs against, so unsaved changes on ``instance`` are ignored.
    :py:func:`log_delete` logs this snapshot once the deletion has happened.

    Direct use is discouraged, connect your model through :py:func:`auditlog.registry.register` instead.
    """
    instance.__dict__.pop(DELETE_SNAPSHOT_ATTR, None)

    pk = instance.pk
    if pk is None:
        return

    manager = sender._base_manager
    if using:
        manager = manager.using(using)

    try:
        persisted = manager.get(pk=pk)
    except sender.DoesNotExist:
        return

    changes = model_instance_diff(persisted, None)
    instance.__dict__[DELETE_SNAPSHOT_ATTR] = (pk, persisted, changes)


def log_delete(sender, instance, **kwargs):
    """
    Signal receiver that creates a log entry when a model instance is deleted from the database.

    The logged values are those captured from the database by :py:func:`snapshot_delete`, never the in-memory state of
    ``instance``. Nothing is logged if no snapshot exists for the primary key that was deleted.

    Direct use is discouraged, connect your model through :py:func:`auditlog.registry.register` instead.
    """
    snapshot = instance.__dict__.pop(DELETE_SNAPSHOT_ATTR, None)
    if snapshot is None or instance.pk is None:
        return

    pk, persisted, changes = snapshot
    if pk != instance.pk:
        return

    log_entry = LogEntry.objects.log_create(
        persisted,
        action=LogEntry.Action.DELETE,
        changes=json.dumps(changes),
    )
