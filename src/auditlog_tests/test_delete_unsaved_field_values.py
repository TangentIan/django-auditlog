"""Delete audit entries must record the row that was stored.

These tests exercise Model.delete() after unsaved assignments. They fail while
log_delete diffs the in-memory instance instead of the persisted row.
"""
import json

from django.contrib.contenttypes.models import ContentType
from django.forms.models import modelform_factory
from django.test import TestCase

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


def _delete_entry(instance, pk):
    content_type = ContentType.objects.get_for_model(instance.__class__)
    return LogEntry.objects.get(
        content_type=content_type,
        object_pk=str(pk),
        action=LogEntry.Action.DELETE,
    )


def _additional_data(entry):
    data = entry.additional_data
    if isinstance(data, str):
        data = json.loads(data)
    return data


class DeleteLogsPersistedRowTests(TestCase):
    def test_instance_delete_logs_persisted_values_not_unsaved_assignments(self):
        obj = SimpleModel.objects.create(
            text='persisted-secret',
            boolean=True,
            integer=7,
        )
        pk = obj.pk
        self.assertEqual(SimpleModel.objects.get(pk=pk).text, 'persisted-secret')
        self.assertEqual(SimpleModel.objects.get(pk=pk).boolean, True)
        self.assertEqual(SimpleModel.objects.get(pk=pk).integer, 7)

        obj.text = 'benign-unsaved'
        obj.boolean = False
        obj.integer = 0
        self.assertEqual(SimpleModel.objects.get(pk=pk).text, 'persisted-secret')
        self.assertEqual(SimpleModel.objects.get(pk=pk).boolean, True)
        self.assertEqual(SimpleModel.objects.get(pk=pk).integer, 7)

        other = SimpleModel.objects.create(text='other-row', boolean=False, integer=1)
        obj.delete()

        self.assertFalse(SimpleModel.objects.filter(pk=pk).exists())
        self.assertTrue(SimpleModel.objects.filter(pk=other.pk).exists())

        entry = _delete_entry(obj, pk)
        changes = entry.changes_dict
        self.assertEqual(changes['text'], ['persisted-secret', 'None'])
        self.assertEqual(changes['boolean'], ['True', 'None'])
        self.assertEqual(changes['integer'], ['7', 'None'])
        self.assertNotEqual(changes['text'][0], 'benign-unsaved')
        self.assertNotEqual(changes['text'][0], 'other-row')
        self.assertNotEqual(changes['boolean'][0], 'False')
        self.assertNotEqual(changes['integer'][0], '0')
        self.assertEqual(
            LogEntry.objects.filter(
                content_type=entry.content_type,
                object_pk=str(pk),
            ).count(),
            1,
        )

    def test_instance_delete_logs_persisted_text_when_unsaved_value_is_empty(self):
        obj = SimpleModel.objects.create(text='persisted-secret', boolean=False, integer=3)
        pk = obj.pk
        obj.text = ''
        self.assertEqual(SimpleModel.objects.get(pk=pk).text, 'persisted-secret')
        obj.delete()

        self.assertFalse(SimpleModel.objects.filter(pk=pk).exists())
        entry = _delete_entry(obj, pk)
        self.assertEqual(entry.changes_dict['text'], ['persisted-secret', 'None'])

    def test_modelform_validation_assigns_then_delete_logs_persisted_values(self):
        obj = SimpleModel.objects.create(
            text='persisted-secret',
            boolean=False,
            integer=4,
        )
        pk = obj.pk
        Form = modelform_factory(SimpleModel, fields=['text', 'boolean', 'integer'])
        form = Form(
            data={'text': 'benign-form', 'boolean': 'on', 'integer': '0'},
            instance=obj,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(obj.text, 'benign-form')
        self.assertEqual(obj.boolean, True)
        self.assertEqual(obj.integer, 0)
        self.assertEqual(SimpleModel.objects.get(pk=pk).text, 'persisted-secret')
        self.assertEqual(SimpleModel.objects.get(pk=pk).boolean, False)
        self.assertEqual(SimpleModel.objects.get(pk=pk).integer, 4)

        obj.delete()

        self.assertFalse(SimpleModel.objects.filter(pk=pk).exists())
        changes = _delete_entry(obj, pk).changes_dict
        self.assertEqual(changes['text'], ['persisted-secret', 'None'])
        self.assertEqual(changes['boolean'], ['False', 'None'])
        self.assertEqual(changes['integer'], ['4', 'None'])

    def test_modelform_save_commit_false_then_delete_logs_persisted_values(self):
        obj = SimpleModel.objects.create(
            text='persisted-secret',
            boolean=True,
            integer=8,
        )
        pk = obj.pk
        Form = modelform_factory(SimpleModel, fields=['text', 'boolean', 'integer'])
        form = Form(
            data={'text': 'benign-commit-false', 'boolean': '', 'integer': '1'},
            instance=obj,
        )
        bound = form.save(commit=False)
        self.assertIs(bound, obj)
        self.assertEqual(obj.text, 'benign-commit-false')
        self.assertEqual(SimpleModel.objects.get(pk=pk).text, 'persisted-secret')
        self.assertEqual(SimpleModel.objects.get(pk=pk).boolean, True)
        self.assertEqual(SimpleModel.objects.get(pk=pk).integer, 8)

        bound.delete()

        self.assertFalse(SimpleModel.objects.filter(pk=pk).exists())
        changes = _delete_entry(obj, pk).changes_dict
        self.assertEqual(changes['text'], ['persisted-secret', 'None'])
        self.assertEqual(changes['boolean'], ['True', 'None'])
        self.assertEqual(changes['integer'], ['8', 'None'])

    def test_char_primary_key_delete_logs_persisted_values(self):
        obj = AltPrimaryKeyModel.objects.create(
            key='row-key',
            text='persisted-secret',
            boolean=True,
            integer=5,
        )
        obj.text = 'benign-unsaved'
        obj.boolean = False
        obj.integer = 0
        self.assertEqual(
            AltPrimaryKeyModel.objects.get(pk='row-key').text,
            'persisted-secret',
        )
        obj.delete()

        self.assertFalse(AltPrimaryKeyModel.objects.filter(pk='row-key').exists())
        changes = _delete_entry(obj, 'row-key').changes_dict
        self.assertEqual(changes['text'], ['persisted-secret', 'None'])
        self.assertEqual(changes['boolean'], ['True', 'None'])
        self.assertEqual(changes['integer'], ['5', 'None'])

    def test_uuid_primary_key_delete_logs_persisted_values(self):
        obj = UUIDPrimaryKeyModel.objects.create(
            text='persisted-secret',
            boolean=False,
            integer=6,
        )
        pk = obj.pk
        obj.text = 'benign-unsaved'
        obj.boolean = True
        obj.integer = 0
        self.assertEqual(UUIDPrimaryKeyModel.objects.get(pk=pk).text, 'persisted-secret')
        obj.delete()

        self.assertFalse(UUIDPrimaryKeyModel.objects.filter(pk=pk).exists())
        changes = _delete_entry(obj, pk).changes_dict
        self.assertEqual(changes['text'], ['persisted-secret', 'None'])
        self.assertEqual(changes['boolean'], ['False', 'None'])
        self.assertEqual(changes['integer'], ['6', 'None'])

    def test_proxy_model_delete_logs_persisted_values(self):
        obj = ProxyModel.objects.create(text='persisted-secret', boolean=True, integer=2)
        pk = obj.pk
        obj.text = 'benign-unsaved'
        obj.boolean = False
        self.assertEqual(ProxyModel.objects.get(pk=pk).text, 'persisted-secret')
        obj.delete()

        self.assertFalse(SimpleModel.objects.filter(pk=pk).exists())
        changes = _delete_entry(obj, pk).changes_dict
        self.assertEqual(changes['text'], ['persisted-secret', 'None'])
        self.assertEqual(changes['boolean'], ['True', 'None'])

    def test_include_and_exclude_fields_use_persisted_values(self):
        included = SimpleIncludeModel.objects.create(
            label='persisted-label',
            text='persisted-body',
        )
        included_pk = included.pk
        included.label = 'benign-label'
        included.text = 'benign-body'
        self.assertEqual(
            SimpleIncludeModel.objects.get(pk=included_pk).label,
            'persisted-label',
        )
        included.delete()

        included_changes = _delete_entry(included, included_pk).changes_dict
        self.assertEqual(included_changes['label'], ['persisted-label', 'None'])
        self.assertNotIn('text', included_changes)

        excluded = SimpleExcludeModel.objects.create(
            label='persisted-label',
            text='persisted-body',
        )
        excluded_pk = excluded.pk
        excluded.label = 'benign-label'
        excluded.text = 'benign-body'
        self.assertEqual(
            SimpleExcludeModel.objects.get(pk=excluded_pk).text,
            'persisted-body',
        )
        excluded.delete()

        excluded_changes = _delete_entry(excluded, excluded_pk).changes_dict
        self.assertEqual(excluded_changes['label'], ['persisted-label', 'None'])
        self.assertNotIn('text', excluded_changes)

    def test_unsaved_foreign_key_and_additional_data_use_persisted_row(self):
        original = SimpleModel.objects.create(text='persisted-related-text')
        other = SimpleModel.objects.create(text='other-related-text')
        child = AdditionalDataIncludedModel.objects.create(
            label='persisted-label',
            text='persisted-body',
            related=original,
        )
        pk = child.pk
        child.label = 'benign-label'
        child.text = 'benign-body'
        child.related = other
        self.assertEqual(
            AdditionalDataIncludedModel.objects.get(pk=pk).related_id,
            original.pk,
        )
        self.assertEqual(
            AdditionalDataIncludedModel.objects.get(pk=pk).label,
            'persisted-label',
        )

        child.delete()

        self.assertFalse(AdditionalDataIncludedModel.objects.filter(pk=pk).exists())
        self.assertTrue(SimpleModel.objects.filter(pk=original.pk).exists())
        entry = _delete_entry(child, pk)
        changes = entry.changes_dict
        self.assertEqual(changes['label'], ['persisted-label', 'None'])
        self.assertEqual(changes['text'], ['persisted-body', 'None'])
        self.assertEqual(changes['related'], [str(original), 'None'])
        self.assertNotEqual(changes['related'][0], str(other))
        additional = _additional_data(entry)
        self.assertEqual(additional['related_model_id'], original.pk)
        self.assertEqual(additional['related_model_text'], 'persisted-related-text')

    def test_cached_related_mutation_is_not_recorded_on_delete(self):
        original = SimpleModel.objects.create(text='persisted-related-text')
        child = AdditionalDataIncludedModel.objects.create(
            label='persisted-label',
            text='persisted-body',
            related=original,
        )
        pk = child.pk
        child = AdditionalDataIncludedModel.objects.get(pk=pk)
        self.assertEqual(child.related.text, 'persisted-related-text')
        child.related.text = 'benign-related-text'
        self.assertEqual(SimpleModel.objects.get(pk=original.pk).text, 'persisted-related-text')

        child.delete()

        entry = _delete_entry(child, pk)
        additional = _additional_data(entry)
        self.assertEqual(additional['related_model_id'], original.pk)
        self.assertEqual(additional['related_model_text'], 'persisted-related-text')
        self.assertEqual(entry.changes_dict['label'], ['persisted-label', 'None'])
        self.assertEqual(entry.changes_dict['text'], ['persisted-body', 'None'])
        self.assertEqual(entry.changes_dict['related'], [str(original), 'None'])
