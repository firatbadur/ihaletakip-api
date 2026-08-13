r"""
OKAS adı trigram indeksini `adi` → `adi_norm`'a taşır (doldurmadan SONRA).

⚠️ **`manage.py backfill_okas_norm` çalıştırılmadan uygulamayın.** Boş kolona trigram
kurup ardından 1,2M satır UPDATE etmek indeksi şişirir; sıra kolon (`0018`) → doldur →
indeks (bu).

Eski indeks (`ekap_okasitem_adi_trgm`, 75 MB) düşürülüyor çünkü filtre artık ham `adi`
kolonuna hiç bakmıyor. Denetimde zaten `idx_scan = 0`'dı — ama sebebi "kimse aramıyor"
değil, `icontains`'in `UPPER(adi)` üretip indeksi kullanılamaz kılmasıydı.

⚠️ `atomic = False` + CONCURRENTLY: hem yeni indeksi kurmak hem eskisini düşürmek
ACCESS EXCLUSIVE kilidi gerektirir; sürekli koşan ingest görevlerinin arkasına kuyruğa
girerse tüm okuyucuları bloklar. Rollback YOK; yarıda kesilirse:
    SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
    DROP INDEX CONCURRENTLY <ad>;  → migrate ekap tekrar
"""
from django.contrib.postgres.indexes import GinIndex
from django.db import migrations

from ._pg_ops import PgAddIndexConcurrently, PgRemoveIndexConcurrently


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0018_okasitem_adi_norm"),
    ]

    operations = [
        PgAddIndexConcurrently(
            model_name="okasitem",
            index=GinIndex(
                fields=["adi_norm"],
                opclasses=["gin_trgm_ops"],
                name="ekap_okasitem_adinorm_trgm",
            ),
        ),
        PgRemoveIndexConcurrently(
            model_name="okasitem",
            name="ekap_okasitem_adi_trgm",
        ),
    ]
