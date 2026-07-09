"""
Ortam değişkenlerinden admin (superuser) oluşturur/günceller.

Docker entrypoint bunu otomatik çağırır. Idempotent'tir:
kullanıcı varsa şifre/staff/superuser durumunu günceller.

Env:
    DJANGO_SUPERUSER_USERNAME (varsayılan: firat)
    DJANGO_SUPERUSER_PASSWORD (varsayılan: Firat1212b.)
    DJANGO_SUPERUSER_EMAIL    (varsayılan: firat@ihaletakip.local)
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Ortam değişkenlerinden superuser oluşturur veya günceller."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "firat")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "Firat1212b.")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "firat@ihaletakip.local")

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "provider": User.Provider.EMAIL},
        )
        user.email = email or user.email
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()

        if created:
            self.stdout.write(self.style.SUCCESS(f"✅ Admin oluşturuldu: {username}"))
        else:
            self.stdout.write(self.style.WARNING(f"♻️  Admin güncellendi: {username}"))
