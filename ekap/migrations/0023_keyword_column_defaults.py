"""
`kalip_hash` / `sektor` kolonlarına **DB seviyesinde** default — üretim arızası düzeltmesi.

⚠️ YAŞANMIŞ ARIZA (2026-08-28 → 08-31, 3 GÜN): 0020/0021 bu kolonları `NOT NULL` olarak
ekledi ama DB tarafında default bırakmadı (Django `AddField`'ın normal davranışı: default
yalnızca mevcut satırları doldurmak için geçici kullanılır, sonra düşürülür). Django
tarafında sorun yoktu — model `CharField(blank=True)` için `''` üretir.

Kırılma noktası **deploy** oldu: migration uygulandı ama yalnızca `web` yeni imaja
geçirildi; worker'lar eski kodda kaldı. Eski `Tender` modeli bu kolonları tanımadığı için
`INSERT` deyimine hiç dahil etmedi → DB'de default olmadığından NULL gitti →
`null value in column "kalip_hash" violates not-null constraint`.

Sonuç: `sync_recent` her turda 225 ihalenin HEPSİNİ düşürdü (`items=0, errors=225`),
üç gün boyunca tek bir yeni ihale kaydedilmedi ve buna bağlı olarak bildirimler de sustu
(filtre/idare alarmları "bugün yayınlanan ihale" arıyor).

⚠️ DERS — model kolonu eklerken İKİSİ birden gerekir:
  1. Kolona DB seviyesinde default ver (bu migration): eski kod yazmaya devam edebilsin.
  2. Deploy'da TÜM servisleri yeni imaja geçir, yalnızca `web`'i değil. Eski worker +
     yeni şema, sessizce veri kaybettiren bir kombinasyondur.

Belirti parmak izi: `SyncRun.status='ok'` ama `items=0, errors=<sayfa dolusu>`. `status`
'ok' olduğu için admin listesinde arıza görünmez; gerçek sebep `note` ve worker log'undaki
"ihale atlandı ikn=… : null value in column …" satırındadır.
"""
from django.db import migrations

from ._pg_ops import PgRunSQL


class Migration(migrations.Migration):

    dependencies = [
        ("ekap", "0022_keyword_indexes"),
    ]

    operations = [
        PgRunSQL(
            """
            ALTER TABLE ekap_tender   ALTER COLUMN kalip_hash SET DEFAULT '';
            ALTER TABLE ekap_tender   ALTER COLUMN sektor     SET DEFAULT '';
            ALTER TABLE ekap_contract ALTER COLUMN sektor     SET DEFAULT '';
            """,
            # Geri alma: default'u düşür (kolonlar NOT NULL kalır).
            """
            ALTER TABLE ekap_tender   ALTER COLUMN kalip_hash DROP DEFAULT;
            ALTER TABLE ekap_tender   ALTER COLUMN sektor     DROP DEFAULT;
            ALTER TABLE ekap_contract ALTER COLUMN sektor     DROP DEFAULT;
            """,
        ),
    ]
