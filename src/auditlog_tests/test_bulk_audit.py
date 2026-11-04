"""Audit entries for queryset writes that bypass Model.save().

These tests fail while bulk_create and QuerySet.update insert or change
registered rows without a LogEntry. They pass once those writes record the
same create/update entry, actor, address, and field diff as save().
"""
import uuid

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils.encoding import smart_text

from auditlog.middleware import AuditlogMiddleware
from auditlog.models import LogEntry
from auditlog_tests.models import (
    AdditionalDataIncludedModel,
    AltPrimaryKeyModel,
    ProxyModel,
    SimpleExcludeModel,
    SimpleIncludeModel,
    SimpleModel,
    UUIDPrimaryKeyModel,
)


class BulkWriteAuditTests(TestCase):
    def setUp(self):
        self.middleware = AuditlogMiddleware()
        self.user = User.objects.create(username='importer')
        self.other_user = User.objects.create(username='someone-else')
        self.request = RequestFactory().get('/', REMOTE_ADDR='198.51.100.10')
        self.request.user = self.user
        self.middleware.process_request(self.request)

    def tearDown(self):
        self.middleware.process_response(self.request, HttpResponse())

    def assert_create_audit(self, obj, expected_new_values, absent_fields=()):
        self.assertIsNotNone(obj.pk)
        entries = obj.history.filter(action=LogEntry.Action.CREATE)
        self.assertEqual(
            entries.count(),
            1,
            'CREATE audit entry missing for %s pk=%s' % (obj.__class__.__name__, obj.pk),
        )
        entry = entries.get()
        self.assertEqual(entry.actor_id, self.user.pk)
        self.assertNotEqual(entry.actor_id, self.other_user.pk)
        self.assertEqual(entry.remote_addr, '198.51.100.10')
        self.assertEqual(entry.object_pk, smart_text(obj.pk))
        self.assertEqual(entry.object_repr, smart_text(obj))
        changes = entry.changes_dict
        for name, new_value in expected_new_values.items():
            self.assertIn(name, changes)
            self.assertEqual(changes[name][0], 'None')
            self.assertEqual(changes[name][1], smart_text(new_value))
        for name in absent_fields:
            self.assertNotIn(name, changes)
        return entry

    def assert_update_audit(self, obj, expected_changes):
        entries = obj.history.filter(action=LogEntry.Action.UPDATE)
        self.assertEqual(
            entries.count(),
            1,
            'UPDATE audit entry missing for %s pk=%s' % (obj.__class__.__name__, obj.pk),
        )
        entry = entries.get()
        self.assertEqual(entry.actor_id, self.user.pk)
        self.assertNotEqual(entry.actor_id, self.other_user.pk)
        self.assertEqual(entry.remote_addr, '198.51.100.10')
        self.assertEqual(entry.object_pk, smart_text(obj.pk))
        changes = entry.changes_dict
        for name, (old_value, new_value) in expected_changes.items():
            self.assertEqual(changes[name], [smart_text(old_value), smart_text(new_value)])
        return entry

    def test_save_records_actor_and_address(self):
        """Model.save still logs the request actor. The bulk tests compare against this path."""
        obj = SimpleModel.objects.create(text='saved-through-model', boolean=True, integer=3)
        self.assert_create_audit(obj, {
            'text': 'saved-through-model',
            'boolean': True,
            'integer': 3,
        })

    def test_manager_bulk_create_records_each_inserted_row(self):
        """Attack path: registered model, many instances, QuerySet.bulk_create via the manager."""
        SimpleModel.objects.bulk_create([
            SimpleModel(text='imported-a', boolean=True, integer=7),
            SimpleModel(text='imported-b', boolean=False, integer=8),
        ])

        saved_a = SimpleModel.objects.get(text='imported-a')
        saved_b = SimpleModel.objects.get(text='imported-b')
        self.assertEqual(saved_a.integer, 7)
        self.assertTrue(saved_a.boolean)
        self.assertEqual(saved_b.integer, 8)
        self.assertFalse(saved_b.boolean)
        self.assert_create_audit(saved_a, {
            'text': 'imported-a',
            'boolean': True,
            'integer': 7,
        })
        self.assert_create_audit(saved_b, {
            'text': 'imported-b',
            'boolean': False,
            'integer': 8,
        })
        self.assertEqual(
            LogEntry.objects.get_for_objects(SimpleModel.objects.filter(text__in=['imported-a', 'imported-b'])).count(),
            2,
        )

    def test_queryset_bulk_create_records_inserted_objects_not_existing_rows(self):
        """bulk_create on a filtered queryset must log the objects argument, not rows the filter matches."""
        already = SimpleModel.objects.create(text='already-there', integer=5)
        already_creates = already.history.filter(action=LogEntry.Action.CREATE).count()

        SimpleModel.objects.filter(integer=5).bulk_create([
            SimpleModel(text='not-in-filter', integer=9),
        ])
        SimpleModel.objects.all().bulk_create([
            SimpleModel(text='from-all-queryset', integer=10),
        ])

        created = SimpleModel.objects.get(text='not-in-filter')
        from_all = SimpleModel.objects.get(text='from-all-queryset')
        self.assertEqual(created.integer, 9)
        self.assert_create_audit(created, {'text': 'not-in-filter', 'integer': 9})
        self.assert_create_audit(from_all, {'text': 'from-all-queryset', 'integer': 10})
        already.refresh_from_db()
        self.assertEqual(already.text, 'already-there')
        self.assertEqual(already.history.filter(action=LogEntry.Action.CREATE).count(), already_creates)
        self.assertEqual(already.history.filter(action=LogEntry.Action.UPDATE).count(), 0)

    def test_batched_bulk_create_records_every_batch(self):
        SimpleModel.objects.bulk_create([
            SimpleModel(text='batch-1', integer=1),
            SimpleModel(text='batch-2', integer=2),
            SimpleModel(text='batch-3', integer=3),
        ], batch_size=1)

        for text, integer in (('batch-1', 1), ('batch-2', 2), ('batch-3', 3)):
            saved = SimpleModel.objects.get(text=text)
            self.assertEqual(saved.integer, integer)
            self.assert_create_audit(saved, {'text': text, 'integer': integer})

    def test_bulk_create_with_explicit_and_uuid_primary_keys(self):
        AltPrimaryKeyModel.objects.bulk_create([
            AltPrimaryKeyModel(key='attacker-key', text='explicit-pk', boolean=True, integer=4),
        ])
        explicit = AltPrimaryKeyModel.objects.get(key='attacker-key')
        self.assertEqual(explicit.text, 'explicit-pk')
        self.assert_create_audit(explicit, {
            'text': 'explicit-pk',
            'boolean': True,
            'integer': 4,
        })

        known_id = uuid.UUID('11111111-2222-3333-4444-555555555555')
        UUIDPrimaryKeyModel.objects.bulk_create([
            UUIDPrimaryKeyModel(id=known_id, text='uuid-pk', integer=6),
        ])
        saved = UUIDPrimaryKeyModel.objects.get(pk=known_id)
        self.assertEqual(saved.text, 'uuid-pk')
        entry = self.assert_create_audit(saved, {'text': 'uuid-pk', 'integer': 6})
        self.assertIsNone(entry.object_id)
        self.assertEqual(entry.object_pk, smart_text(known_id))

    def test_bulk_create_honors_include_and_exclude_fields(self):
        SimpleIncludeModel.objects.bulk_create([
            SimpleIncludeModel(label='visible-label', text='hidden-text'),
        ])
        included = SimpleIncludeModel.objects.get(label='visible-label')
        self.assertEqual(included.text, 'hidden-text')
        self.assert_create_audit(
            included,
            {'label': 'visible-label'},
            absent_fields=('text',),
        )

        SimpleExcludeModel.objects.bulk_create([
            SimpleExcludeModel(label='kept-label', text='dropped-text'),
        ])
        excluded = SimpleExcludeModel.objects.get(label='kept-label')
        self.assertEqual(excluded.text, 'dropped-text')
        self.assert_create_audit(
            excluded,
            {'label': 'kept-label'},
            absent_fields=('text',),
        )

    def test_bulk_create_stores_additional_data_from_the_inserted_instance(self):
        related = SimpleModel.objects.create(text='related-row')
        AdditionalDataIncludedModel.objects.bulk_create([
            AdditionalDataIncludedModel(label='with-extra', text='body', related=related),
        ])
        saved = AdditionalDataIncludedModel.objects.get(label='with-extra')
        self.assertEqual(saved.related_id, related.pk)
        entry = self.assert_create_audit(saved, {'label': 'with-extra', 'text': 'body'})
        self.assertEqual(entry.additional_data, {
            'related_model_id': related.pk,
            'related_model_text': 'related-row',
        })

    def test_proxy_model_bulk_create_records_the_proxy_row(self):
        ProxyModel.objects.bulk_create([
            ProxyModel(text='proxy-import', boolean=True, integer=11),
        ])
        saved = ProxyModel.objects.get(text='proxy-import')
        self.assertEqual(saved.integer, 11)
        self.assert_create_audit(saved, {
            'text': 'proxy-import',
            'boolean': True,
            'integer': 11,
        })

    def test_queryset_update_records_each_changed_row(self):
        """QuerySet.update writes SQL without pre_save/post_save, so the same gap drops UPDATE entries."""
        alpha = SimpleModel.objects.create(text='alpha', integer=1, boolean=False)
        beta = SimpleModel.objects.create(text='beta', integer=2, boolean=False)
        untouched = SimpleModel.objects.create(text='untouched', integer=3, boolean=False)

        updated = SimpleModel.objects.filter(pk__in=[alpha.pk, beta.pk]).update(text='same', integer=9)
        self.assertEqual(updated, 2)

        alpha.refresh_from_db()
        beta.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(alpha.text, 'same')
        self.assertEqual(alpha.integer, 9)
        self.assertEqual(beta.text, 'same')
        self.assertEqual(beta.integer, 9)
        self.assertEqual(untouched.text, 'untouched')
        self.assert_update_audit(alpha, {'text': ('alpha', 'same'), 'integer': (1, 9)})
        self.assert_update_audit(beta, {'text': ('beta', 'same'), 'integer': (2, 9)})
        self.assertEqual(untouched.history.filter(action=LogEntry.Action.UPDATE).count(), 0)

    def test_queryset_update_same_value_does_not_add_an_entry(self):
        obj = SimpleModel.objects.create(text='stable', integer=4)
        before = obj.history.count()
        SimpleModel.objects.filter(pk=obj.pk).update(text='stable')
        obj.refresh_from_db()
        self.assertEqual(obj.text, 'stable')
        self.assertEqual(obj.history.count(), before)
        self.assertEqual(obj.history.filter(action=LogEntry.Action.UPDATE).count(), 0)

    def test_empty_bulk_create_writes_no_log_entry(self):
        before = LogEntry.objects.count()
        created = SimpleModel.objects.bulk_create([])
        self.assertEqual(list(created), [])
        self.assertEqual(LogEntry.objects.count(), before)

    def test_failed_bulk_create_writes_no_row_and_no_log_entry(self):
        before = LogEntry.objects.count()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AltPrimaryKeyModel.objects.bulk_create([
                    AltPrimaryKeyModel(key='duplicate-key', text='first'),
                    AltPrimaryKeyModel(key='duplicate-key', text='second'),
                ])
        self.assertFalse(AltPrimaryKeyModel.objects.filter(key='duplicate-key').exists())
        self.assertEqual(LogEntry.objects.count(), before)
