"""
Security spec: audited field values must not be stored unbounded, and the
audit-log admin must not load or parse a full changes document per row.

These tests fail while that behavior is present. They are expected to pass
once serialized values are capped before insert and the changelist (and the
change form) no longer reads the entire changes document.

Bound: one LogEntry.changes value is at most 64 KiB for a single text field,
however large that field is. Django's default list page is 100 rows
(ModelAdmin.list_per_page), so an uncapped multi-megabyte value on each row
is what exhausts the worker. 64 KiB keeps a full page in the low megabytes
even when both the previous and the new value are kept.

Not covered here:
- LogEntry.changes_dict, changes_str, and changes_display_dict. They also
  json.loads the stored document, but the admin list and change form do not
  call them. The storage cap is what keeps those properties bounded for rows
  written after the fix.
- A separate JSONField. Every non-datetime field, including a JSON value,
  goes through get_field_value's smart_text branch and the same diff loop as
  the TextField exercised below.
"""
import json

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
try:
    from django.urls import reverse
except ImportError:
    from django.core.urlresolvers import reverse

from auditlog.mixins import LogEntryAdminMixin
from auditlog.models import LogEntry
import auditlog.mixins as auditlog_mixins
import auditlog.models as auditlog_models
from auditlog_tests.models import SimpleModel


# One text field, old and new both kept, plus JSON syntax, must fit in this.
MAX_CHANGES_BYTES = 64 * 1024
# Under Django's default DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MiB), so a normal
# request can already plant a value this large.
PAYLOAD_CHARS = 1000 * 1000


def _quote(name):
    return connection.ops.quote_name(name)


class _MaterializedChanges(object):
    """Record changes-document sizes loaded or parsed during a request."""

    def __init__(self):
        self.loaded = []
        self.parsed = []
        self._original_from_db = None
        self._original_mixin_loads = None
        self._original_model_loads = None

    def __enter__(self):
        self._original_from_db = LogEntry.from_db.__func__
        self._original_mixin_loads = auditlog_mixins.json.loads
        self._original_model_loads = auditlog_models.json.loads
        loaded = self.loaded
        parsed = self.parsed
        original_from_db = self._original_from_db

        def from_db(cls, db, field_names, values):
            names = list(field_names)
            if 'changes' in names:
                value = list(values)[names.index('changes')]
                if isinstance(value, (str, bytes)):
                    loaded.append(len(value))
            return original_from_db(cls, db, field_names, values)

        def spy_loads(original):
            def loads(payload, *args, **kwargs):
                if isinstance(payload, (str, bytes)):
                    parsed.append(len(payload))
                return original(payload, *args, **kwargs)
            return loads

        LogEntry.from_db = classmethod(from_db)
        auditlog_mixins.json.loads = spy_loads(self._original_mixin_loads)
        auditlog_models.json.loads = spy_loads(self._original_model_loads)
        return self

    def __exit__(self, exc_type, exc, tb):
        LogEntry.from_db = classmethod(self._original_from_db)
        auditlog_mixins.json.loads = self._original_mixin_loads
        auditlog_models.json.loads = self._original_model_loads
        return False


def _assert_changes_bounded(test, entry):
    entry = LogEntry.objects.get(pk=entry.pk)
    test.assertLessEqual(
        len(entry.changes),
        MAX_CHANGES_BYTES,
        'LogEntry %s stored %s bytes of changes for one text field'
        % (entry.pk, len(entry.changes)),
    )
    parsed = json.loads(entry.changes)
    test.assertIn('text', parsed)
    test.assertEqual(len(parsed['text']), 2)
    for value in parsed['text']:
        test.assertIsInstance(value, str)
        test.assertLessEqual(len(value), MAX_CHANGES_BYTES)


def _plant_poisoned_rows(count):
    """
    Insert log rows whose changes documents are already multi-hundred-kilobyte
    JSON, using SQL so a later save() or field prep hook cannot shrink them.
    This is the state left behind by the unbounded write path.
    """
    payload = 'Q' * PAYLOAD_CHARS
    document = json.dumps({'text': [payload, payload]})
    content_type = ContentType.objects.get_for_model(SimpleModel)
    rows = []
    for index in range(count):
        entry = LogEntry.objects.create(
            content_type=content_type,
            object_pk=str(index),
            object_id=index,
            object_repr='poisoned-row-%s' % index,
            action=LogEntry.Action.UPDATE,
            changes='{}',
        )
        rows.append(entry)
    table = _quote(LogEntry._meta.db_table)
    changes_column = _quote(LogEntry._meta.get_field('changes').column)
    pk_column = _quote(LogEntry._meta.pk.column)
    with connection.cursor() as cursor:
        for entry in rows:
            cursor.execute(
                'UPDATE %s SET %s = %%s WHERE %s = %%s' % (
                    table, changes_column, pk_column,
                ),
                [document, entry.pk],
            )
    return rows, len(document)


class BoundedChangeStorageTests(TestCase):
    def test_create_stores_a_bounded_changes_document(self):
        obj = SimpleModel.objects.create(text='A' * PAYLOAD_CHARS)
        entry = obj.history.get(action=LogEntry.Action.CREATE)
        _assert_changes_bounded(self, entry)

    def test_update_stores_bounded_previous_and_new_values(self):
        obj = SimpleModel.objects.create(text='A' * PAYLOAD_CHARS)
        obj.text = 'B' * PAYLOAD_CHARS
        obj.save()
        entry = obj.history.get(action=LogEntry.Action.UPDATE)
        _assert_changes_bounded(self, entry)

    def test_delete_stores_a_bounded_changes_document(self):
        obj = SimpleModel.objects.create(text='C' * PAYLOAD_CHARS)
        content_type = ContentType.objects.get_for_model(SimpleModel)
        object_pk = str(obj.pk)
        obj.delete()
        entry = LogEntry.objects.get(
            content_type=content_type,
            object_pk=object_pk,
            action=LogEntry.Action.DELETE,
        )
        _assert_changes_bounded(self, entry)


class AdminChangesParsingTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create(
            username='audit-staff',
            is_staff=True,
            is_superuser=True,
        )
        self.staff.set_unusable_password()
        self.staff.save()
        self.client.force_login(self.staff)

    def _assert_request_does_not_materialize_changes(self, url, marker):
        with _MaterializedChanges() as seen:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, marker)
        loaded_max = max(seen.loaded) if seen.loaded else 0
        parsed_max = max(seen.parsed) if seen.parsed else 0
        self.assertLessEqual(
            loaded_max,
            MAX_CHANGES_BYTES,
            'admin materialized changes: loaded_max=%s parsed_max=%s bytes'
            % (loaded_max, parsed_max),
        )
        self.assertLessEqual(
            parsed_max,
            MAX_CHANGES_BYTES,
            'admin json.loads changes: loaded_max=%s parsed_max=%s bytes'
            % (loaded_max, parsed_max),
        )
        return response, seen

    def test_changelist_after_real_large_updates_does_not_parse_full_documents(self):
        # Attack path: save a large text value, save another so the log holds
        # both, then a staff member opens the audit log changelist.
        for _ in range(3):
            obj = SimpleModel.objects.create(text='A' * PAYLOAD_CHARS)
            obj.text = 'B' * PAYLOAD_CHARS
            obj.save()
        url = reverse('admin:auditlog_logentry_changelist')
        self._assert_request_does_not_materialize_changes(url, 'text')

    def test_changelist_search_does_not_parse_full_documents(self):
        rows, document_bytes = _plant_poisoned_rows(4)
        self.assertGreater(document_bytes, MAX_CHANGES_BYTES)
        url = reverse('admin:auditlog_logentry_changelist') + '?q=text'
        response, _seen = self._assert_request_does_not_materialize_changes(
            url, 'poisoned-row-0',
        )
        for row in rows:
            self.assertContains(response, row.object_repr)

    def test_poisoned_rows_are_not_fully_loaded_or_parsed_on_the_changelist(self):
        rows, document_bytes = _plant_poisoned_rows(4)
        self.assertGreater(document_bytes, MAX_CHANGES_BYTES)
        url = reverse('admin:auditlog_logentry_changelist')
        response, _seen = self._assert_request_does_not_materialize_changes(
            url, 'poisoned-row-0',
        )
        for row in rows:
            self.assertContains(response, row.object_repr)

    def test_change_form_does_not_load_or_embed_a_full_changes_document(self):
        rows, document_bytes = _plant_poisoned_rows(1)
        self.assertGreater(document_bytes, MAX_CHANGES_BYTES)
        url = reverse('admin:auditlog_logentry_change', args=[rows[0].pk])
        with _MaterializedChanges() as seen:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        loaded_max = max(seen.loaded) if seen.loaded else 0
        parsed_max = max(seen.parsed) if seen.parsed else 0
        self.assertLessEqual(
            loaded_max,
            MAX_CHANGES_BYTES,
            'change form materialized changes: loaded_max=%s parsed_max=%s bytes'
            % (loaded_max, parsed_max),
        )
        self.assertLessEqual(
            parsed_max,
            MAX_CHANGES_BYTES,
            'change form json.loads changes: loaded_max=%s parsed_max=%s bytes'
            % (loaded_max, parsed_max),
        )
        body = response.content.decode('utf-8')
        # Longer than a short display truncation, far shorter than the payload.
        self.assertEqual(
            body.find('Q' * 10000),
            -1,
            'change form embedded at least 10000 payload characters',
        )


class MsgShortUsesFieldNamesOnlyTests(TestCase):
    """msg_short is the changelist column. Calling it must not parse a full document."""

    def test_msg_short_does_not_parse_an_existing_large_document(self):
        rows, document_bytes = _plant_poisoned_rows(1)
        self.assertGreater(document_bytes, MAX_CHANGES_BYTES)
        # Reload through the same queryset the changelist uses: every column.
        entry = LogEntry.objects.get(pk=rows[0].pk)
        mixin = LogEntryAdminMixin()
        with _MaterializedChanges() as seen:
            text = mixin.msg_short(entry)
        self.assertIn('text', text)
        parsed_max = max(seen.parsed) if seen.parsed else 0
        self.assertLessEqual(
            parsed_max,
            MAX_CHANGES_BYTES,
            'msg_short json.loads()ed %s bytes' % parsed_max,
        )

    def test_msg_does_not_parse_or_embed_an_existing_large_document(self):
        rows, document_bytes = _plant_poisoned_rows(1)
        entry = LogEntry.objects.get(pk=rows[0].pk)
        mixin = LogEntryAdminMixin()
        with _MaterializedChanges() as seen:
            html = mixin.msg(entry)
        parsed_max = max(seen.parsed) if seen.parsed else 0
        self.assertLessEqual(
            parsed_max,
            MAX_CHANGES_BYTES,
            'msg json.loads()ed %s bytes' % parsed_max,
        )
        self.assertEqual(
            html.find('Q' * 10000),
            -1,
            'msg embedded at least 10000 payload characters',
        )
