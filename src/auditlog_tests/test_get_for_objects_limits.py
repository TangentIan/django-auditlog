"""History lookups must keep working after the audited set no longer fits
in one SQL statement.

``LogEntryManager.get_for_objects`` loads every primary key into a Python
list and binds that list in a single ``IN`` predicate. These tests build a
queryset larger than the database's bound-variable limit and then read the
history the way ``docs/source/usage.rst`` tells applications to read it.
They fail while that single predicate is still in place.
"""
from django.db import connection
from django.db.utils import OperationalError
from django.test import TestCase
from django.utils.encoding import smart_text

from auditlog.models import LogEntry
from auditlog_tests.models import (
    AltPrimaryKeyModel,
    ManyRelatedModel,
    SimpleModel,
    UUIDPrimaryKeyModel,
)


# Stay under SQLite's compile-time SQLITE_MAX_COMPOUND_SELECT (500). Models
# whose insert has no concrete columns ignore Django's bulk batch size and
# otherwise emit one compound SELECT for the whole batch.
BULK_BATCH = 400


def sqlite_bound_variable_limit():
    """Largest number of bound parameters this connection actually accepts."""
    if connection.vendor != 'sqlite':
        raise RuntimeError('These tests run against the sqlite test database.')

    def accepts(count):
        sql = 'SELECT 1 WHERE 1 IN (%s)' % ','.join(['%s'] * count)
        cursor = connection.cursor()
        try:
            cursor.execute(sql, [1] * count)
        except OperationalError as exc:
            if 'too many SQL variables' not in smart_text(exc):
                raise
            return False
        else:
            return True
        finally:
            cursor.close()

    low = 1
    high = 2
    while accepts(high):
        low = high
        high *= 2
        if high > 1000000:
            raise RuntimeError('SQLite did not enforce a bound-variable limit.')
    while low + 1 < high:
        mid = (low + high) // 2
        if accepts(mid):
            low = mid
        else:
            high = mid
    return low


def bulk_create(model, rows):
    for offset in range(0, len(rows), BULK_BATCH):
        model.objects.bulk_create(rows[offset:offset + BULK_BATCH], batch_size=BULK_BATCH)


class GetForObjectsVariableLimitTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super(GetForObjectsVariableLimitTests, cls).setUpClass()
        cls.variable_limit = sqlite_bound_variable_limit()

    def _assert_history_contains(self, queryset, expected_entries):
        self.assertGreater(queryset.count(), self.variable_limit)
        found = list(LogEntry.objects.get_for_objects(queryset))
        for entry in expected_entries:
            self.assertIn(entry, found)

    def test_integer_primary_keys_over_variable_limit(self):
        early = SimpleModel.objects.create(text='early')
        bulk_create(SimpleModel, [SimpleModel(text='filler') for _ in range(self.variable_limit + 1)])
        late = SimpleModel.objects.create(text='late')

        self._assert_history_contains(
            SimpleModel.objects.all(),
            [early.history.get(), late.history.get()],
        )

    def test_documented_m2m_relation_over_variable_limit(self):
        """The many-to-many example in the usage docs calls get_for_objects
        on ``obj.related.all()`` and then reads that queryset."""
        parent = ManyRelatedModel.objects.create()
        early = ManyRelatedModel.objects.create()
        through = ManyRelatedModel.related.through
        through.objects.create(
            from_manyrelatedmodel_id=parent.pk,
            to_manyrelatedmodel_id=early.pk,
        )
        before = ManyRelatedModel.objects.order_by('-pk').values_list('pk', flat=True).first()
        bulk_create(ManyRelatedModel, [ManyRelatedModel() for _ in range(self.variable_limit + 1)])
        filler_ids = list(
            ManyRelatedModel.objects.filter(pk__gt=before).values_list('pk', flat=True)
        )
        bulk_create(through, [
            through(from_manyrelatedmodel_id=parent.pk, to_manyrelatedmodel_id=pk)
            for pk in filler_ids
        ])
        late = ManyRelatedModel.objects.create()
        through.objects.create(
            from_manyrelatedmodel_id=parent.pk,
            to_manyrelatedmodel_id=late.pk,
        )

        # Same call as docs/source/usage.rst (Many-to-many relationships).
        rel_history = LogEntry.objects.get_for_objects(parent.related.all())
        self.assertGreater(parent.related.count(), self.variable_limit)
        found = list(rel_history)
        self.assertIn(early.history.get(), found)
        self.assertIn(late.history.get(), found)

    def test_through_model_queryset_over_variable_limit(self):
        parent = ManyRelatedModel.objects.create()
        targets = []
        # Two real rows at each end, plus enough links to overflow one IN list.
        for _ in range(2):
            target = ManyRelatedModel.objects.create()
            targets.append(target)
        through = ManyRelatedModel.related.through
        through.objects.create(
            from_manyrelatedmodel_id=parent.pk,
            to_manyrelatedmodel_id=targets[0].pk,
        )
        before = ManyRelatedModel.objects.order_by('-pk').values_list('pk', flat=True).first()
        bulk_create(ManyRelatedModel, [ManyRelatedModel() for _ in range(self.variable_limit + 1)])
        filler_ids = list(
            ManyRelatedModel.objects.filter(pk__gt=before).values_list('pk', flat=True)
        )
        bulk_create(through, [
            through(from_manyrelatedmodel_id=parent.pk, to_manyrelatedmodel_id=pk)
            for pk in filler_ids
        ])
        through.objects.create(
            from_manyrelatedmodel_id=parent.pk,
            to_manyrelatedmodel_id=targets[1].pk,
        )
        related_links = through.objects.filter(from_manyrelatedmodel_id=parent.pk)
        # Auto-created through models do not emit save signals, so write the
        # log rows the history lookup is supposed to return.
        first_link = related_links.order_by('pk').first()
        last_link = related_links.order_by('-pk').first()
        first_entry = LogEntry.objects.log_create(
            first_link, action=LogEntry.Action.CREATE, changes='{}',
        )
        last_entry = LogEntry.objects.log_create(
            last_link, action=LogEntry.Action.CREATE, changes='{}',
        )
        self._assert_history_contains(related_links, [first_entry, last_entry])

    def test_uuid_primary_keys_over_variable_limit(self):
        early = UUIDPrimaryKeyModel.objects.create(text='early')
        bulk_create(
            UUIDPrimaryKeyModel,
            [UUIDPrimaryKeyModel(text='filler') for _ in range(self.variable_limit + 1)],
        )
        late = UUIDPrimaryKeyModel.objects.create(text='late')

        self._assert_history_contains(
            UUIDPrimaryKeyModel.objects.all(),
            [early.history.get(), late.history.get()],
        )

    def test_char_primary_keys_over_variable_limit(self):
        early = AltPrimaryKeyModel.objects.create(key='sentinel-early', text='early')
        bulk_create(AltPrimaryKeyModel, [
            AltPrimaryKeyModel(key='filler-%08d' % index, text='filler')
            for index in range(self.variable_limit + 1)
        ])
        late = AltPrimaryKeyModel.objects.create(key='sentinel-late', text='late')

        self._assert_history_contains(
            AltPrimaryKeyModel.objects.all(),
            [early.history.get(), late.history.get()],
        )
