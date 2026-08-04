"""Celery application for theorem_control.

Queues
------
- default (`celery`): Python offload tasks
- `offload.r`: R runtime tasks (rpy2); agent name on provenance activities is "R"
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "theorem_control.settings")

app = Celery("theorem_control")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, name="theorem_control.debug_task")
def debug_task(self) -> str:
    return f"request={self.request!r}"
