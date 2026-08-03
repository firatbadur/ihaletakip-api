r"""
Firma (yüklenici) arama + sıralama indeksleri.

Ölçüm (üretim, 0007 sonrası): `/ekap/tenders/?yuklenici=…` **sıcak cache'te bile
1.15 sn**. Sebep: `Exists` alt sorgusu `Contract.yuklenici_adi_norm` ve
`Contractor.arama_norm` üzerinde `%…%` LIKE yapıyor, ikisinde de yalnızca düz btree
(`db_index=True`) var — baştan jokerli LIKE'ta btree işe yaramaz, trigram gerekir.

Ayrıca `ContractorListView` sıralaması ile mevcut indeksler **uyumsuzdu**: view
`F(alan).desc(nulls_last=True), "kanonik_ad"` ile sıralıyor, `0006`'daki indeksler ise
düz `-alan` (Postgres'te DESC **NULLS FIRST**). Pathkey eşleşmediği için planlayıcı
onları sıralama için hiç kullanamıyor, her istekte tüm tabloyu sort ediyordu. İkincil
anahtar `kanonik_ad` da tek kolonluk indeksle karşılanamaz. Buradaki üç ifade indeksi
sorgunun birebir karşılığıdır.

⚠️ `atomic = False` + CONCURRENTLY + yeniden çalıştırılabilirlik: bkz. `_pg_ops`.
⚠️ Deploy: `docker compose stop web nginx` → tek seferlik konteynerde migrate →
   `docker compose up -d --build` (bkz. CLAUDE.md "atomic=False + CONCURRENTLY").
"""
import django.contrib.postgres.indexes
from django.db import migrations, models

from ._pg_ops import PgAddIndexConcurrently


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0007_search_indexes"),
    ]

    operations = [
        # ── Metin araması (%…% LIKE → trigram şart) ──
        PgAddIndexConcurrently(
            model_name="contract",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["yuklenici_adi_norm"],
                name="ekap_contract_yukadi_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        PgAddIndexConcurrently(
            model_name="contractor",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["arama_norm"],
                name="ekap_contractor_arama_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        # ── Firma listesinin ORDER BY'ının birebir karşılığı (nulls_last + kanonik_ad) ──
        PgAddIndexConcurrently(
            model_name="contractor",
            index=models.Index(
                models.OrderBy(
                    models.F("sozlesme_sayisi"), descending=True, nulls_last=True
                ),
                models.F("kanonik_ad"),
                name="ekap_ctr_soz_ad_idx",
            ),
        ),
        PgAddIndexConcurrently(
            model_name="contractor",
            index=models.Index(
                models.OrderBy(
                    models.F("toplam_sozlesme_bedeli"), descending=True, nulls_last=True
                ),
                models.F("kanonik_ad"),
                name="ekap_ctr_bedel_ad_idx",
            ),
        ),
        PgAddIndexConcurrently(
            model_name="contractor",
            index=models.Index(
                models.OrderBy(
                    models.F("son_sozlesme_tarihi"), descending=True, nulls_last=True
                ),
                models.F("kanonik_ad"),
                name="ekap_ctr_tarih_ad_idx",
            ),
        ),
    ]
