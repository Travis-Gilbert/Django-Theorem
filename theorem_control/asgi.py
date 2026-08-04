"""ASGI config for theorem_control."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "theorem_control.settings")

application = get_asgi_application()
