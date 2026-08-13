r"""
Pro sorgu şekilleri için indeksler — **prod'da EXPLAIN ile kanıtlandıktan sonra**.

Ölçüm 2026-08-13 (1.031.670 ihale / **11 GB**, 1.407.319 sözleşme / 1 GB):

| Sorgu şekli                          | Önce      | Sebep                                    |
|--------------------------------------|-----------|------------------------------------------|
| benchmark kademe 3 (okas, ülke)      | **4559 ms** | `ekap_tender`'da tam Seq Scan (1,7 GB)  |
| benchmark kademe 1 (okas + idare)    | **4578 ms** | aynı                                     |
| seri tespiti (`seri_anahtar` GROUP BY)| **2492 ms** | Seq Scan + diske taşan sort (3×20 MB)   |

⚠️ **Neden şimdi, daha önce değil:** kolonlar (`okas_ana_kod`, `en_ust_idare_kod`,
`seri_anahtar`) 2026-08-07'ye kadar dolduruluyordu. Boş kolona indeks kurup ardından
~1M satır UPDATE etmek indeksi şişirirdi. Sıra: kolon → doldur → indeks.
`Contract` kopyaları için de aynı sıra: `0014` (kolon) →
`manage.py backfill_contract_fields` (doldur) → bu migration.

⚠️ **Kısmi indeks (`WHERE okas_ana_kod <> ''`) bilinçli olarak KULLANILMADI.** Sorgu
`okas_ana_kod = %s` biçiminde parametrelidir; planlayıcı parametrenin `''` olmadığını
plan zamanında **kanıtlayamaz** ve kısmi indeksi kullanamazdı. Boş değerlerin (~%15)
indekste yer kaplaması, indeksin hiç kullanılmamasından iyidir.

⚠️ Sıralama kolonu (`-ihale_tarihi` / `-sozlesme_tarihi`) bileşiğin **İKİNCİ**
elemanıdır. Baş kolon yapılsaydı `ORDER BY tarih DESC LIMIT N` + seçici filtre
tuzağının ta kendisi olurdu (bu kod tabanını üç kez ısırdı) — eşitlik baş kolonda
olunca planlayıcı dar bir aralık tarayıp sıralı çıktıyı bedavaya alır.

⚠️ `atomic = False` + CONCURRENTLY: rollback YOK. Uzun açık transaction'ları BEKLER →
migration boyunca admin → Periodic Tasks'tan `ekap-sync-contractors`, `ekap-backfill`
ve `ekap-backfill-tender-fields` kapatın. Yarıda kesilirse:
    SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
    DROP INDEX CONCURRENTLY <ad>;  → migrate ekap tekrar
Bitince: ANALYZE ekap_tender; ANALYZE ekap_contract;
"""
from django.db import migrations, models

from ._pg_ops import PgAddIndexConcurrently


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0014_contract_okas_bakanlik"),
    ]

    operations = [
        # ── Tender: Pro arama filtreleri (eşitlik + tarih sıralaması) ──────────
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                fields=["okas_ana_kod", "-ihale_tarihi"],
                name="ekap_tender_okas_tarih_idx",
            ),
        ),
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                fields=["en_ust_idare_kod", "-ihale_tarihi"],
                name="ekap_tender_bakanlik_tarih_idx",
            ),
        ),
        # Seri tespiti haftalık `GROUP BY seri_anahtar, idare_id` yapıyor; sıralı
        # indeks taraması diske taşan external merge sort'u ortadan kaldırır.
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                fields=["seri_anahtar", "idare_id"],
                name="ekap_tender_seri_idare_idx",
            ),
        ),
        # ── Contract: benchmark benzerlik merdiveni ───────────────────────────
        # Kademe 1-3 (idare / il / ülke) hepsi `okas_ana_kod` eşitliğiyle başlar;
        # idare/il o dar aralıkta ucuz heap filtresidir (kod başına ~300 satır).
        PgAddIndexConcurrently(
            model_name="contract",
            index=models.Index(
                fields=["okas_ana_kod", "-sozlesme_tarihi"],
                name="ekap_contract_okas_tarih_idx",
            ),
        ),
        # Kademe 4: OKAS grubu + ihale türü (OKAS'ı olmayan %19 için kademe 4b
        # mevcut `idare_id` indeksini kullanır, ek indeks gerekmez).
        PgAddIndexConcurrently(
            model_name="contract",
            index=models.Index(
                fields=["okas_bucket", "ihale_tip", "-sozlesme_tarihi"],
                name="ekap_contract_bucket_tip_idx",
            ),
        ),
        # İdare profilinin bakanlık kapsamı — artık `tender__` JOIN'i yapmadan.
        PgAddIndexConcurrently(
            model_name="contract",
            index=models.Index(
                fields=["en_ust_idare_kod", "-sozlesme_tarihi"],
                name="ekap_contract_bakanlik_idx",
            ),
        ),
    ]
