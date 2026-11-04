class SqliteRouter(object):
    """Skip Postgres-only test models when the suite runs on SQLite."""

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name and model_name.startswith('postgres'):
            return False
        return None
