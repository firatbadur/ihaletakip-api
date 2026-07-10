"""Statik dosya storage sınıfları."""
from whitenoise.storage import CompressedManifestStaticFilesStorage


class JazzminManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Manifest'te olmayan yolları hataya çevirmeden servis eder.

    jazzmin'in `admin/base.html` şablonu, karanlık mod tema tabanını
    `{% static 'vendor/bootswatch' %}` ile üretir — bu bir dosya değil DİZİN'dir.
    Manifest yalnızca dosyaları bildiği için katı modda `ValueError: Missing
    staticfiles manifest entry` atar ve tüm admin iç sayfaları 500 döner.

    `manifest_strict = False` ile bilinmeyen yol hash'siz haliyle döner;
    collectstatic hash'siz kopyaları da bıraktığından dosya yine bulunur.
    """

    manifest_strict = False
