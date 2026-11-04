"""SQLite-only settings so the datetime audit tests do not need PostgreSQL."""
from auditlog_tests.test_settings import *  # noqa: F401,F403


class SqliteTestRouter(object):
    """Skip PostgreSQL-only models. The project router assumes model_name is set."""

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name and model_name.startswith('postgres'):
            return False
        return None


DATABASES = {
    'default': DATABASES['default'],  # noqa: F405
}
DATABASE_ROUTERS = ['auditlog_tests.sqlite_settings.SqliteTestRouter']
