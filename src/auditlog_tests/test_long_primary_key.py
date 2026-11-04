# -*- coding: utf-8 -*-
"""Audited models whose primary key is longer than LogEntry.object_pk.

LogEntry.object_pk is a varchar(255). These tests are the expected behavior
for an audited model that allows a longer string primary key: create, update,
and delete must finish, and each one must store that primary key on a log
entry. They fail while a too-long object_pk is rejected by the database.
"""
from __future__ import unicode_literals

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db import models
from django.test import TransactionTestCase

from auditlog.models import LogEntry
from auditlog.registry import auditlog


OBJECT_PK_MAX_LENGTH = 255
FITTING_PK = "k" * OBJECT_PK_MAX_LENGTH
LONG_PK = "k" * (OBJECT_PK_MAX_LENGTH + 1)


class LongPrimaryKeyModel(models.Model):
    """Business model whose own primary key is allowed to exceed 255 characters."""

    key = models.CharField(max_length=1024, primary_key=True)
    text = models.TextField(blank=True)

    class Meta:
        app_label = "auditlog_tests"


auditlog.register(LongPrimaryKeyModel)


class LongPrimaryKeyAuditTests(TransactionTestCase):
    """Exercise create, update, and delete for a primary key the log cannot store."""

    @classmethod
    def setUpClass(cls):
        super(LongPrimaryKeyAuditTests, cls).setUpClass()
        if connection.vendor != "postgresql":
            raise RuntimeError(
                "These tests must run on PostgreSQL (settings auditlog_tests.long_pk_settings). "
                "SQLite accepts values longer than CharField.max_length, so it cannot show this failure."
            )

    def _content_type(self):
        return ContentType.objects.get_for_model(LongPrimaryKeyModel)

    def _insert_without_audit(self, pk, text):
        """Persist a row without logging so update and delete can be tested on their own."""
        auditlog.unregister(LongPrimaryKeyModel)
        try:
            return LongPrimaryKeyModel.objects.create(key=pk, text=text)
        finally:
            auditlog.register(LongPrimaryKeyModel)

    def _assert_log(self, pk, action):
        entries = list(LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=pk,
            action=action,
        ))
        self.assertEqual(
            len(entries),
            1,
            "expected one %s log entry for object_pk of length %s, found %s"
            % (action, len(pk), len(entries)),
        )
        self.assertEqual(entries[0].object_pk, pk)
        return entries[0]

    def test_primary_key_of_255_characters_can_be_created_updated_and_deleted(self):
        """A primary key that fits object_pk is audited through create, update, and delete."""
        obj = LongPrimaryKeyModel.objects.create(key=FITTING_PK, text="created")
        self._assert_log(FITTING_PK, LogEntry.Action.CREATE)

        obj.text = "updated"
        obj.save()
        self.assertEqual(LongPrimaryKeyModel.objects.get(pk=FITTING_PK).text, "updated")
        self._assert_log(FITTING_PK, LogEntry.Action.UPDATE)

        obj.delete()
        self.assertFalse(LongPrimaryKeyModel.objects.filter(pk=FITTING_PK).exists())
        self._assert_log(FITTING_PK, LogEntry.Action.DELETE)

    def test_create_with_primary_key_longer_than_255_persists_and_is_audited(self):
        """objects.create() must commit the row and a create log entry, or leave neither."""
        error = None
        obj = None
        try:
            obj = LongPrimaryKeyModel.objects.create(key=LONG_PK, text="created")
        except Exception as exc:
            error = exc

        row_exists = LongPrimaryKeyModel.objects.filter(pk=LONG_PK, text="created").exists()
        create_logs = LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=LONG_PK,
            action=LogEntry.Action.CREATE,
        ).count()
        if error is not None or not row_exists or create_logs != 1:
            self.fail(
                "create of a %s-character primary key raised %r; "
                "business row left in place: %s; create audit entries stored under that pk: %s"
                % (len(LONG_PK), error, row_exists, create_logs)
            )

        self.assertEqual(
            LogEntry.objects.get_for_object(obj).filter(action=LogEntry.Action.CREATE).count(),
            1,
        )

    def test_save_updates_row_with_primary_key_longer_than_255_and_is_audited(self):
        """Model.save() on an existing long-pk row must persist the change and log it."""
        obj = self._insert_without_audit(LONG_PK, "before")
        obj.text = "updated"
        error = None
        try:
            obj.save()
        except Exception as exc:
            error = exc

        stored = LongPrimaryKeyModel.objects.filter(pk=LONG_PK).values_list("text", flat=True).first()
        update_logs = LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=LONG_PK,
            action=LogEntry.Action.UPDATE,
        ).count()
        if error is not None or stored != "updated" or update_logs != 1:
            self.fail(
                "save() of a %s-character primary key raised %r; stored text: %r; update audit entries: %s"
                % (len(LONG_PK), error, stored, update_logs)
            )
        self.assertEqual(
            LogEntry.objects.get_for_object(obj).filter(action=LogEntry.Action.UPDATE).count(),
            1,
        )

    def test_save_update_fields_updates_row_with_primary_key_longer_than_255(self):
        """save(update_fields=...) uses the same pre_save logging path as save()."""
        obj = self._insert_without_audit(LONG_PK, "before")
        obj.text = "updated"
        error = None
        try:
            obj.save(update_fields=["text"])
        except Exception as exc:
            error = exc

        stored = LongPrimaryKeyModel.objects.filter(pk=LONG_PK).values_list("text", flat=True).first()
        update_logs = LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=LONG_PK,
            action=LogEntry.Action.UPDATE,
        ).count()
        if error is not None or stored != "updated" or update_logs != 1:
            self.fail(
                "save(update_fields) of a %s-character primary key raised %r; stored text: %r; update audit entries: %s"
                % (len(LONG_PK), error, stored, update_logs)
            )

    def test_update_or_create_updates_row_with_primary_key_longer_than_255(self):
        """update_or_create() saves through the same pre_save logging path."""
        self._insert_without_audit(LONG_PK, "before")
        error = None
        try:
            LongPrimaryKeyModel.objects.update_or_create(key=LONG_PK, defaults={"text": "updated"})
        except Exception as exc:
            error = exc

        stored = LongPrimaryKeyModel.objects.filter(pk=LONG_PK).values_list("text", flat=True).first()
        update_logs = LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=LONG_PK,
            action=LogEntry.Action.UPDATE,
        ).count()
        if error is not None or stored != "updated" or update_logs != 1:
            self.fail(
                "update_or_create() of a %s-character primary key raised %r; stored text: %r; update audit entries: %s"
                % (len(LONG_PK), error, stored, update_logs)
            )

    def test_get_or_create_with_primary_key_longer_than_255_persists_and_is_audited(self):
        """get_or_create() inserts through Model.save() and must audit that insert."""
        error = None
        obj = None
        try:
            obj, created = LongPrimaryKeyModel.objects.get_or_create(key=LONG_PK, defaults={"text": "created"})
        except Exception as exc:
            error = exc
            created = False

        row_exists = LongPrimaryKeyModel.objects.filter(pk=LONG_PK, text="created").exists()
        create_logs = LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=LONG_PK,
            action=LogEntry.Action.CREATE,
        ).count()
        if error is not None or not created or not row_exists or create_logs != 1:
            self.fail(
                "get_or_create() of a %s-character primary key raised %r; created=%s; "
                "business row left in place: %s; create audit entries: %s"
                % (len(LONG_PK), error, created, row_exists, create_logs)
            )
        self.assertEqual(
            LogEntry.objects.get_for_object(obj).filter(action=LogEntry.Action.CREATE).count(),
            1,
        )

    def test_instance_delete_removes_row_with_primary_key_longer_than_255_and_is_audited(self):
        """Model.delete() must remove the row. A log-insert error must not restore it."""
        obj = self._insert_without_audit(LONG_PK, "doomed")
        error = None
        try:
            obj.delete()
        except Exception as exc:
            error = exc

        row_exists = LongPrimaryKeyModel.objects.filter(pk=LONG_PK).exists()
        delete_logs = LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=LONG_PK,
            action=LogEntry.Action.DELETE,
        ).count()
        if error is not None or row_exists or delete_logs != 1:
            self.fail(
                "instance delete of a %s-character primary key raised %r; "
                "business row still present: %s; delete audit entries: %s"
                % (len(LONG_PK), error, row_exists, delete_logs)
            )

    def test_queryset_delete_removes_row_with_primary_key_longer_than_255_and_is_audited(self):
        """QuerySet.delete() uses the same collector and post_delete logging as Model.delete()."""
        self._insert_without_audit(LONG_PK, "doomed")
        error = None
        try:
            LongPrimaryKeyModel.objects.filter(pk=LONG_PK).delete()
        except Exception as exc:
            error = exc

        row_exists = LongPrimaryKeyModel.objects.filter(pk=LONG_PK).exists()
        delete_logs = LogEntry.objects.filter(
            content_type=self._content_type(),
            object_pk=LONG_PK,
            action=LogEntry.Action.DELETE,
        ).count()
        if error is not None or row_exists or delete_logs != 1:
            self.fail(
                "queryset delete of a %s-character primary key raised %r; "
                "business row still present: %s; delete audit entries: %s"
                % (len(LONG_PK), error, row_exists, delete_logs)
            )
