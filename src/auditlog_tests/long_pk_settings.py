"""PostgreSQL settings for the long primary-key audit tests.

The default test settings use SQLite, which does not enforce CharField
``max_length``. These tests have to run on a backend that does.
"""
import getpass

from auditlog_tests.test_settings import *  # noqa: F401,F403

DATABASE_ROUTERS = []

# Django 2.1.15 with psycopg2 2.9 raises "database connection isn't set to UTC"
# when it loads LogEntry.timestamp and USE_TZ is on. These tests assert
# primary-key logging, not timezone conversion.
USE_TZ = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'auditlog_tests_db',
        'USER': getpass.getuser(),
        'PASSWORD': '',
        'HOST': '',
        'PORT': '',
    },
}
