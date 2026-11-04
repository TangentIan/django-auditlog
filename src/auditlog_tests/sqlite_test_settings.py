from auditlog_tests.test_settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    },
}

DATABASE_ROUTERS = ['auditlog_tests.sqlite_router.SqliteRouter']
