"""WSGI config for theorem_control."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "theorem_control.settings")

application = get_wsgi_application()
