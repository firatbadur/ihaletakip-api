r"""
`order=ilan_tarihi` sıralaması için ifadeli indeks.

⚠️⚠️ Arıza (üretimde ölçüldü, 2026-09-22): `GET /ekap/tenders/?order=ilan_tarihi&
siralamaTipi=desc` "en yeni ilan" yerine ilan tarihi **boş** kayıtları döndürüyordu.
Sebep Postgres'in varsayılanı: `ORDER BY x DESC` ⇒ **NULLS FIRST**. `ilan_tarihi`
kolonunun %48,2'si NULL (506.902/1.051.946) olduğu için ilk yarım milyon satır
tarihsiz kayıtlardı — mobilde "sıralama hatalı çalışıyor" diye görünen şey buydu.

Düzeltme `ORDER BY ilan_tarihi DESC NULLS LAST, id DESC` (bkz. `views.tender_sira_ifadesi`).
⚠️ Bu sorgu mevcut `db_index` btree'siyle **karşılanamaz**: o indeks `DESC NULLS FIRST`
sırasındadır, `NULLS LAST` istendiğinde sıralı çıktı veremez. Ölçülen plan (indekssiz):

    Limit → Gather Merge (4 worker) → Sort → Parallel Seq Scan on ekap_tender

yani her istek 1M satırı tarayıp sıralıyordu. İfadeli indeks sıralamayı tamamen kaldırır.

⚠️ `id DESC` tie-break indeksin İÇİNDE: `ilan_tarihi` damgası gün başıdır (saat
taşımaz) ve tek günü 897 ihale paylaşıyor. Tie-break'siz `ORDER BY` bu grupların iç
sırasını plana bırakır; `OFFSET` tabanlı sayfalamada sonuç aynı ihalenin iki sayfada
tekrarı ve başkasının hiç görünmemesidir. Tie-break'i indekse koymak sort adımını da
bedava kaldırır.

⚠️ ASC yönü için ikinci bir indeks GEREKMEZ — `ASC NULLS LAST` Postgres'in varsayılan
ASC sırasıdır, mevcut `db_index` ileri taramayla karşılar (ölçüldü: Incremental Sort +
Index Scan). `ihale_tarihi` sıralamasına da dokunulmaz: o kolonda NULL yok (ölçüldü: 0)
ve `NULLS LAST` istemek `(il_id, -ihale_tarihi)` gibi tüm bileşik indeksleri devre dışı
bırakırdı — bu yüzden `views` orada nulls_last KULLANMAZ.

⚠️ `atomic = False` + CONCURRENTLY + yeniden çalıştırılabilirlik: bkz. `_pg_ops`.
⚠️ CONCURRENTLY açık uzun transaction'ları BEKLER; kurulum asılırsa admin → Periodic
Tasks'tan ingest görevlerini geçici kapatın (bkz. CLAUDE.md, 0007 notu).
"""
from django.db import migrations, models
from django.db.models import F

from ._pg_ops import PgAddIndexConcurrently


class Migration(migrations.Migration):
    # CONCURRENTLY transaction içinde çalışamaz → rollback YOK (bkz. _pg_ops).
    atomic = False

    dependencies = [("ekap", "0026_contractor_pending_index")]

    operations = [
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                F("ilan_tarihi").desc(nulls_last=True),
                F("id").desc(),
                name="ekap_tender_ilantarih_sira",
            ),
        ),
    ]
