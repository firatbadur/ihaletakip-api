"""config paketi — Django başlarken Celery app'i yüklenir."""
from .celery import app as celery_app

__all__ = ("celery_app",)
