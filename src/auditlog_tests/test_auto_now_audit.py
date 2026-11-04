"""
Audit entries for values Django writes in Field.pre_save.

log_update runs on pre_save and diffs the in-memory instance. DateTimeField,
DateField, and TimeField assign auto_now inside Field.pre_save, which Model._save_table
calls only after that signal. These tests require an UPDATE LogEntry whose stored
old/new values match the row before the save and the row after Django has written it.

They fail while that diff still runs on the pre-save instance.
"""
import datetime

from django.conf import settings
from django.db.models import DateTimeField
from django.test import TestCase
from django.utils import timezone
from django.utils.encoding import smart_text

from auditlog.models import LogEntry
from auditlog_tests.models import (
    AltPrimaryKeyModel,
    AutoNowExcludeModel,
    AutoNowFieldsModel,
    AutoNowIncludeModel,
    ProxyModel,
    SimpleModel,
    UUIDPrimaryKeyModel,
)

PAST_DATETIME = datetime.datetime(2010, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
PAST_DATE = datetime.date(2010, 1, 2)
PAST_TIME = datetime.time(1, 2, 3)
ASSIGNED_DATETIME = datetime.datetime(2011, 6, 7, 8, 9, 10, tzinfo=timezone.utc)


def stored_change_text(field, value):
    """Reproduce the string model_instance_diff stores for a field value."""
    if isinstance(field, DateTimeField):
        value = field.to_python(value)
        if value is not None and settings.USE_TZ and not timezone.is_naive(value):
            value = timezone.make_naive(value, timezone=timezone.utc)
    return smart_text(value)


class AutoNowAuditTests(TestCase):
    def assertUpdateRecords(self, obj, before, after, expected_fields):
        entries = list(obj.history.filter(action=LogEntry.Action.UPDATE).order_by('pk'))
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.object_pk, smart_text(obj.pk))
        changes = entry.changes_dict
        self.assertEqual(set(changes), set(expected_fields))
        for name in expected_fields:
            field = obj._meta.get_field(name)
            self.assertEqual(
                changes[name],
                [stored_change_text(field, before[name]), stored_change_text(field, after[name])],
            )
            self.assertNotEqual(changes[name][0], changes[name][1])

    def test_touch_save_records_auto_now_datetime(self):
        obj = SimpleModel.objects.create(text='tracked')
        other = SimpleModel.objects.create(text='untouched')
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        SimpleModel.objects.filter(pk=other.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        other.refresh_from_db()
        before_datetime = obj.datetime
        other_before = other.datetime
        other_updates = other.history.filter(action=LogEntry.Action.UPDATE).count()

        obj.save()

        obj.refresh_from_db()
        other.refresh_from_db()
        self.assertNotEqual(obj.datetime, before_datetime)
        self.assertEqual(obj.text, 'tracked')
        self.assertEqual(other.datetime, other_before)
        self.assertEqual(other.history.filter(action=LogEntry.Action.UPDATE).count(), other_updates)
        self.assertUpdateRecords(
            obj,
            {'datetime': before_datetime},
            {'datetime': obj.datetime},
            ['datetime'],
        )

    def test_other_field_update_records_auto_now_datetime(self):
        obj = SimpleModel.objects.create(text='tracked', boolean=False)
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        before_datetime = obj.datetime

        obj.boolean = True
        obj.save()

        obj.refresh_from_db()
        self.assertTrue(obj.boolean)
        self.assertNotEqual(obj.datetime, before_datetime)
        self.assertUpdateRecords(
            obj,
            {'boolean': False, 'datetime': before_datetime},
            {'boolean': True, 'datetime': obj.datetime},
            ['boolean', 'datetime'],
        )

    def test_assigned_auto_now_value_records_value_written(self):
        obj = SimpleModel.objects.create(text='tracked', boolean=False)
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        before_datetime = obj.datetime

        obj.datetime = ASSIGNED_DATETIME
        obj.boolean = True
        obj.save()

        obj.refresh_from_db()
        self.assertNotEqual(obj.datetime, ASSIGNED_DATETIME)
        self.assertNotEqual(obj.datetime, before_datetime)
        self.assertUpdateRecords(
            obj,
            {'boolean': False, 'datetime': before_datetime},
            {'boolean': True, 'datetime': obj.datetime},
            ['boolean', 'datetime'],
        )

    def test_update_fields_limited_to_auto_now_records_change(self):
        obj = SimpleModel.objects.create(text='kept', boolean=False)
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        before_datetime = obj.datetime

        obj.save(update_fields=['datetime'])

        obj.refresh_from_db()
        self.assertEqual(obj.text, 'kept')
        self.assertFalse(obj.boolean)
        self.assertNotEqual(obj.datetime, before_datetime)
        self.assertUpdateRecords(
            obj,
            {'datetime': before_datetime},
            {'datetime': obj.datetime},
            ['datetime'],
        )

    def test_update_fields_with_other_field_records_auto_now(self):
        obj = SimpleModel.objects.create(text='kept', boolean=False)
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        before_datetime = obj.datetime

        obj.boolean = True
        obj.save(update_fields=['boolean', 'datetime'])

        obj.refresh_from_db()
        self.assertTrue(obj.boolean)
        self.assertEqual(obj.text, 'kept')
        self.assertNotEqual(obj.datetime, before_datetime)
        self.assertUpdateRecords(
            obj,
            {'boolean': False, 'datetime': before_datetime},
            {'boolean': True, 'datetime': obj.datetime},
            ['boolean', 'datetime'],
        )

    def test_force_update_records_auto_now(self):
        obj = SimpleModel.objects.create(text='tracked')
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        before_datetime = obj.datetime

        obj.save(force_update=True)

        obj.refresh_from_db()
        self.assertNotEqual(obj.datetime, before_datetime)
        self.assertUpdateRecords(
            obj,
            {'datetime': before_datetime},
            {'datetime': obj.datetime},
            ['datetime'],
        )

    def test_deferred_save_records_loaded_auto_now(self):
        obj = SimpleModel.objects.create(text='kept')
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        deferred = SimpleModel.objects.defer('text').get(pk=obj.pk)
        before_datetime = deferred.datetime

        deferred.save()

        deferred.refresh_from_db()
        self.assertEqual(deferred.text, 'kept')
        self.assertNotEqual(deferred.datetime, before_datetime)
        self.assertUpdateRecords(
            deferred,
            {'datetime': before_datetime},
            {'datetime': deferred.datetime},
            ['datetime'],
        )

    def test_update_or_create_records_auto_now(self):
        obj = SimpleModel.objects.create(text='tracked', boolean=False)
        SimpleModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        before_datetime = obj.datetime

        updated, created = SimpleModel.objects.update_or_create(
            pk=obj.pk,
            defaults={'boolean': True},
        )

        self.assertFalse(created)
        updated.refresh_from_db()
        self.assertTrue(updated.boolean)
        self.assertNotEqual(updated.datetime, before_datetime)
        self.assertUpdateRecords(
            updated,
            {'boolean': False, 'datetime': before_datetime},
            {'boolean': True, 'datetime': updated.datetime},
            ['boolean', 'datetime'],
        )

    def test_date_time_and_extra_datetime_auto_now_fields_are_recorded(self):
        obj = AutoNowFieldsModel.objects.create(label='tracked')
        created_on = obj.created_on
        AutoNowFieldsModel.objects.filter(pk=obj.pk).update(
            touched_on=PAST_DATETIME,
            also_touched_on=PAST_DATETIME,
            stamped_on=PAST_DATE,
            clocked_at=PAST_TIME,
        )
        obj.refresh_from_db()
        before = {
            'touched_on': obj.touched_on,
            'also_touched_on': obj.also_touched_on,
            'stamped_on': obj.stamped_on,
            'clocked_at': obj.clocked_at,
        }

        obj.label = 'changed'
        obj.save()

        obj.refresh_from_db()
        self.assertEqual(obj.created_on, created_on)
        self.assertEqual(obj.label, 'changed')
        for name, previous in before.items():
            self.assertNotEqual(getattr(obj, name), previous)
        self.assertUpdateRecords(
            obj,
            dict(before, label='tracked'),
            {
                'label': 'changed',
                'touched_on': obj.touched_on,
                'also_touched_on': obj.also_touched_on,
                'stamped_on': obj.stamped_on,
                'clocked_at': obj.clocked_at,
            },
            ['label', 'touched_on', 'also_touched_on', 'stamped_on', 'clocked_at'],
        )

    def test_touch_save_records_every_changed_auto_now_field(self):
        obj = AutoNowFieldsModel.objects.create(label='tracked')
        created_on = obj.created_on
        AutoNowFieldsModel.objects.filter(pk=obj.pk).update(
            touched_on=PAST_DATETIME,
            also_touched_on=PAST_DATETIME,
            stamped_on=PAST_DATE,
            clocked_at=PAST_TIME,
        )
        obj.refresh_from_db()
        before = {
            'touched_on': obj.touched_on,
            'also_touched_on': obj.also_touched_on,
            'stamped_on': obj.stamped_on,
            'clocked_at': obj.clocked_at,
        }

        obj.save()

        obj.refresh_from_db()
        self.assertEqual(obj.label, 'tracked')
        self.assertEqual(obj.created_on, created_on)
        self.assertUpdateRecords(
            obj,
            before,
            {name: getattr(obj, name) for name in before},
            list(before),
        )

    def test_include_fields_records_auto_now_and_exclude_fields_omits_it(self):
        included = AutoNowIncludeModel.objects.create(label='kept', ignored='secret')
        AutoNowIncludeModel.objects.filter(pk=included.pk).update(touched_on=PAST_DATETIME)
        included.refresh_from_db()
        before_touched = included.touched_on

        included.save()

        included.refresh_from_db()
        self.assertEqual(included.ignored, 'secret')
        self.assertNotEqual(included.touched_on, before_touched)
        self.assertUpdateRecords(
            included,
            {'touched_on': before_touched},
            {'touched_on': included.touched_on},
            ['touched_on'],
        )

        excluded = AutoNowExcludeModel.objects.create(label='before')
        AutoNowExcludeModel.objects.filter(pk=excluded.pk).update(touched_on=PAST_DATETIME)
        excluded.refresh_from_db()
        excluded_before = excluded.touched_on

        excluded.label = 'after'
        excluded.save()

        excluded.refresh_from_db()
        self.assertEqual(excluded.label, 'after')
        self.assertNotEqual(excluded.touched_on, excluded_before)
        self.assertUpdateRecords(
            excluded,
            {'label': 'before'},
            {'label': 'after'},
            ['label'],
        )

    def test_proxy_touch_save_records_auto_now(self):
        obj = ProxyModel.objects.create(text='proxy')
        ProxyModel.objects.filter(pk=obj.pk).update(datetime=PAST_DATETIME)
        obj.refresh_from_db()
        before_datetime = obj.datetime

        obj.save()

        obj.refresh_from_db()
        self.assertNotEqual(obj.datetime, before_datetime)
        self.assertUpdateRecords(
            obj,
            {'datetime': before_datetime},
            {'datetime': obj.datetime},
            ['datetime'],
        )

    def test_non_integer_pk_touch_save_records_auto_now(self):
        alt = AltPrimaryKeyModel.objects.create(key='alt-key', text='alt')
        AltPrimaryKeyModel.objects.filter(pk=alt.pk).update(datetime=PAST_DATETIME)
        alt.refresh_from_db()
        alt_before = alt.datetime

        uuid_obj = UUIDPrimaryKeyModel.objects.create(text='uuid')
        UUIDPrimaryKeyModel.objects.filter(pk=uuid_obj.pk).update(datetime=PAST_DATETIME)
        uuid_obj.refresh_from_db()
        uuid_before = uuid_obj.datetime

        alt.save()
        uuid_obj.save()

        alt.refresh_from_db()
        uuid_obj.refresh_from_db()
        self.assertNotEqual(alt.datetime, alt_before)
        self.assertNotEqual(uuid_obj.datetime, uuid_before)
        self.assertUpdateRecords(
            alt,
            {'datetime': alt_before},
            {'datetime': alt.datetime},
            ['datetime'],
        )
        self.assertUpdateRecords(
            uuid_obj,
            {'datetime': uuid_before},
            {'datetime': uuid_obj.datetime},
            ['datetime'],
        )
