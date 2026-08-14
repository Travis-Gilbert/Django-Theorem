"""pytest configuration for the Django-Theorem control plane.

pytest-django supplies the isolated test database and applies migrations. Do
not override its ``django_db_setup`` fixture with a no-op: database-backed
contract tests depend on the control-schema tables being present.
"""
