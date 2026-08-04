import pytest


@pytest.fixture(scope="session")
def django_db_setup():
    """Use the project settings DATABASES (SQLite by default)."""
    pass
