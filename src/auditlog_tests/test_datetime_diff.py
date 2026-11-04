"""
Specs for DateTimeField auditing.

These tests fail while get_field_value compares wall-clock datetimes. They pass
once both sides are compared as the instant Django will persist.
"""
import datetime
import json

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from auditlog.models import LogEntry
from auditlog_tests.models import DateTimeFieldModel


def _stored_utc_wall(obj):
    """Return the UTC wall time written for obj.timestamp, without the ORM converter."""
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT "timestamp" FROM %s WHERE id = %%s' % connection.ops.quote_name(obj._meta.db_table),
            [obj.pk],
        )
        raw = cursor.fetchone()[0]
    if isinstance(raw, str):
        raw = parse_datetime(raw)
    if timezone.is_aware(raw):
        raw = timezone.make_naive(raw, timezone.utc)
    return raw


class DatetimeDiffAuditTests(TestCase):
    """Update audits for DateTimeField must follow the instant Django persists."""

    def _create(self, timestamp):
        return DateTimeFieldModel.objects.create(
            label='audited datetime',
            timestamp=timestamp,
            date=datetime.date(2020, 6, 1),
            time=datetime.time(18, 0),
        )

    def test_naive_wall_matching_stored_utc_is_a_real_change(self):
        """A naive value whose wall clock matches the stored UTC wall clock is a later instant.

        USE_TZ is on and TIME_ZONE is not UTC, so Django interprets the naive
        value in the default zone and writes a different instant. The update
        must be audited.
        """
        with self.settings(USE_TZ=True, TIME_ZONE='America/Chicago'):
            stored = datetime.datetime(2020, 6, 1, 18, 0, tzinfo=timezone.utc)
            naive_same_wall = datetime.datetime(2020, 6, 1, 18, 0)
            expected = timezone.make_aware(naive_same_wall, timezone.get_default_timezone())
            self.assertNotEqual(expected, stored)
            expected_wall = timezone.make_naive(expected, timezone.utc)

            obj = self._create(stored)
            self.assertEqual(_stored_utc_wall(obj), datetime.datetime(2020, 6, 1, 18, 0))
            create_count = obj.history.filter(action=LogEntry.Action.CREATE).count()
            self.assertEqual(create_count, 1)

            obj.timestamp = naive_same_wall
            obj.save()

            obj.refresh_from_db()
            self.assertEqual(obj.timestamp, expected)
            self.assertEqual(_stored_utc_wall(obj), expected_wall)

            updates = obj.history.filter(action=LogEntry.Action.UPDATE)
            self.assertEqual(
                updates.count(),
                1,
                'The persisted instant changed and no update LogEntry was written.',
            )
            changes = json.loads(updates.get().changes)
            self.assertIn('timestamp', changes)
            self.assertNotEqual(changes['timestamp'][0], changes['timestamp'][1])

    def test_naive_value_for_the_same_instant_is_not_an_update(self):
        """The local wall time of the stored instant is not a change.

        Comparing the naive value to the UTC wall clock would record an update
        even though Django writes the same instant back.
        """
        with self.settings(USE_TZ=True, TIME_ZONE='America/Chicago'):
            naive = datetime.datetime(2020, 6, 1, 18, 0)
            stored = timezone.make_aware(naive, timezone.get_default_timezone())
            stored_wall = timezone.make_naive(stored, timezone.utc)

            obj = self._create(stored)
            self.assertEqual(_stored_utc_wall(obj), stored_wall)

            obj.timestamp = naive
            obj.save()

            obj.refresh_from_db()
            self.assertEqual(obj.timestamp, stored)
            self.assertEqual(_stored_utc_wall(obj), stored_wall)
            self.assertEqual(
                obj.history.filter(action=LogEntry.Action.UPDATE).count(),
                0,
                'An update LogEntry was written for a datetime whose instant did not change.',
            )
