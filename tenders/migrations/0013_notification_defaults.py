"""
`Notification.filtre_idler` / `idare_detsis_liste` kolonlarına **DB seviyesinde** default.

⚠️⚠️ 0012 bu kolonları `NOT NULL` ekledi ama DB tarafında default bırakmadı — Django
`AddField`'ın normal davranışı: `default=""` yalnızca mevcut satırları doldurmak için
geçici kullanılır, sonra düşürülür. Yani model seviyesinde `default=""` yazmak **DB
seviyesinde default DEMEK DEĞİL**; üretimde `information_schema` ile doğrulandı:
`column_default` boş, `is_nullable = NO`.

Bu, `ekap/migrations/0023_keyword_column_defaults.py`'de belgelenen **2026-08-28 arızasının
(3 GÜN veri kaybı)** birebir kurulumu: migration uygulanır, bir servis eski imajda kalır,
eski model kolonu tanımadığı için `INSERT` deyimine hiç koymaz → NULL gider →
`null value in column ... violates not-null constraint` → satırlar sessizce düşer.
Burada düşecek satır **bildirim**dir: kullanıcı hiç bildirim almaz, `SyncRun` temiz görünür.

⚠️ `install.sh` tüm servisleri birlikte yenilediği için pencere kısa; ama "kısa pencere"
bir güvence değil — o arıza da tam böyle başladı. Kolon eklerken İKİSİ birden gerekir:
DB default + tüm servislerin yeni imaja geçmesi.
"""
from django.db import migrations

from ekap.migrations._pg_ops import PgRunSQL


class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0012_notification_birlesik_ozet"),
    ]

    operations = [
        PgRunSQL(
            """
            ALTER TABLE tenders_notification ALTER COLUMN filtre_idler       SET DEFAULT '';
            ALTER TABLE tenders_notification ALTER COLUMN idare_detsis_liste SET DEFAULT '';
            """,
            # Geri alma: default'u düşür (kolonlar NOT NULL kalır).
            """
            ALTER TABLE tenders_notification ALTER COLUMN filtre_idler       DROP DEFAULT;
            ALTER TABLE tenders_notification ALTER COLUMN idare_detsis_liste DROP DEFAULT;
            """,
        ),
    ]
