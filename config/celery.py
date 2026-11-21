"""Celery app; tasks live in apps/collect/tasks.py, the beat schedule in settings."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("fleet")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
