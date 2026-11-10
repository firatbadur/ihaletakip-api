from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Hesaplar"

    def ready(self):
        from . import signals  # noqa: F401  (yeni kullanıcı → hoş geldin e-postası)
