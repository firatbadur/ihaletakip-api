"""Blog botunun veri analizi yazıları için kategori.

Bot bilinmeyen kategoride 400 alır; admin'den elle açılmasını beklemek ilk analiz
yazısını düşürürdü.
"""
from django.db import migrations


def ekle(apps, schema_editor):
    apps.get_model("blog", "BlogCategory").objects.get_or_create(
        slug="analiz", defaults={"name": "Analiz", "order": 3})


class Migration(migrations.Migration):

    dependencies = [("blog", "0002_blogpost_external_id")]

    operations = [migrations.RunPython(ekle, migrations.RunPython.noop)]
