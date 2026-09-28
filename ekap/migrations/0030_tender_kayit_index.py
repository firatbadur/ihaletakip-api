r"""
`Tender.created_at` (kayıt tarihi) indeksi.

Kayıtlı filtre bildirimi 2026-09-28'de **kayıt tarihine** geçti: "son 24 saatte
sistemimize giren ihaleler". Sorgu şekli
`WHERE created_at >= now()-24h AND created_at <= now() AND <filtre> ORDER BY created_at
DESC LIMIT 300`.

⚠️⚠️ İndeks **performans değil doğruluk/dayanıklılık** gerekçesiyle kuruluyor: seçici bir
aralık + `ORDER BY ... LIMIT` bu kod tabanında üç kez ölçülmüş bir plan tuzağıdır (0009,
0026, 0027). Planlayıcı sıralama+LIMIT'i görünce mevcut bir tarih indeksini **geriye
tarayıp** her satırı heap'ten elemeyi seçiyor; eşleşen satır seyrek olduğunda tabloyu
bitirmek zorunda kalıyor. 2026-09-14'te aynı desen 8 yetim sorgu ve sunucu genelinde
%90 iowait üretmişti.

24 saatlik pencere ~200/1.000.000 satır seçer (ölçüldü 2026-09-28: günde 174-288 kayıt,
hepsi 00:09-02:33 arasında) → indeks aralığı okur, 200 satır döndürür, sıralama da aynı
indeksten bedava gelir.

⚠️ `atomic = False` + CONCURRENTLY + yeniden çalıştırılabilirlik: bkz. `_pg_ops`.
⚠️ CONCURRENTLY açık uzun transaction'ları BEKLER; kurulum asılırsa admin → Periodic
Tasks'tan ingest görevlerini geçici kapatın (bkz. CLAUDE.md, 0007 notu).
"""
from django.db import migrations, models

from ._pg_ops import PgAddIndexConcurrently


class Migration(migrations.Migration):
    # CONCURRENTLY transaction içinde çalışamaz → rollback YOK (bkz. _pg_ops).
    atomic = False

    dependencies = [("ekap", "0029_seri_donem_pazar_boyut")]

    operations = [
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(fields=["-created_at"], name="ekap_tender_kayit_idx"),
        ),
    ]
