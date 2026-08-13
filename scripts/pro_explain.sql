-- ═══════════════════════════════════════════════════════════════════════
--  Pro sorgu şekilleri — indeks kararı ÖNCESİ ölçüm
--
--  Kullanım (host'tan):
--    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
--      < scripts/pro_explain.sql
--
--  ⚠️ Bu kod tabanında indeks EKLEMEDEN ÖNCE EXPLAIN kuralı vardır: `ORDER BY <tarih>
--     DESC LIMIT N` + seçici filtre üç kez plan tuzağına düşürdü (ihale detay ucu,
--     _tender_idare_id_set, enqueue_missing_detail). Bkz. CLAUDE.md.
--
--  Okuma anahtarı: her sorgunun sonundaki "Execution Time" ve plan tipi.
--    • "Index Scan Backward using ekap_tender_ihale_tarihi..." + yüksek "Rows Removed
--      by Filter"  → PLAN TUZAĞI: tarih indeksini geriye tarayıp filtreliyor.
--    • "Seq Scan on ekap_tender"                            → indeks yok.
--    • "Bitmap Heap Scan" / dar "Index Scan"                → sağlıklı.
--  `Buffers: shared read=` yüksekse veri diskten geliyor (soğuk cache) — iki kez
--  çalıştırıp ikincisine bakın.
-- ═══════════════════════════════════════════════════════════════════════

\timing on

\echo '=== 0. Tablo boyutlari ==='
SELECT relname,
       to_char(n_live_tup, 'FM999,999,999') AS satir,
       pg_size_pretty(pg_total_relation_size(relid)) AS toplam
FROM pg_stat_user_tables
WHERE relname IN ('ekap_tender','ekap_contract','ekap_okasitem','ekap_contractor')
ORDER BY pg_total_relation_size(relid) DESC;

\echo ''
\echo '=== 1. Pro TUTAR ARALIGI + ORDER BY tarih DESC LIMIT (plan tuzagi adayi) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, ikn FROM ekap_tender
WHERE yaklasik_maliyet_num >= 1000000 AND yaklasik_maliyet_num <= 5000000
ORDER BY ihale_tarihi DESC
LIMIT 20;

\echo ''
\echo '=== 2. OKAS ana kod esitligi + ORDER BY tarih DESC LIMIT ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, ikn FROM ekap_tender
WHERE okas_ana_kod = (
    SELECT okas_ana_kod FROM ekap_tender
    WHERE okas_ana_kod <> '' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
)
ORDER BY ihale_tarihi DESC
LIMIT 20;

\echo ''
\echo '=== 3. Bakanlik rollup (en_ust_idare_kod) + ORDER BY tarih DESC LIMIT ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, ikn FROM ekap_tender
WHERE en_ust_idare_kod = (
    SELECT en_ust_idare_kod FROM ekap_tender
    WHERE en_ust_idare_kod <> '' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
)
ORDER BY ihale_tarihi DESC
LIMIT 20;

\echo ''
\echo '=== 4. Gerceklikci kombinasyon: sonuclanmis + tutar araligi + siralama ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, ikn FROM ekap_tender
WHERE sozlesme_sayisi > 0
  AND toplam_sozlesme_bedeli >= 500000 AND toplam_sozlesme_bedeli <= 10000000
ORDER BY ihale_tarihi DESC
LIMIT 20;

\echo ''
\echo '=== 5. BENCHMARK kademe 3 — Contract JOIN Tender (Contract tarafinda okas yok) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS n, avg(c.indirim_orani) AS ort
FROM ekap_contract c
JOIN ekap_tender t ON t.id = c.tender_id
WHERE t.okas_ana_kod = (
    SELECT okas_ana_kod FROM ekap_tender
    WHERE okas_ana_kod <> '' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
)
  AND c.sozlesme_tarihi >= now() - interval '5 years';

\echo ''
\echo '=== 6. BENCHMARK kademe 1 — ayni idare + OKAS (JOIN''li) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS n, avg(c.indirim_orani) AS ort
FROM ekap_contract c
JOIN ekap_tender t ON t.id = c.tender_id
WHERE t.okas_ana_kod = (
    SELECT okas_ana_kod FROM ekap_tender
    WHERE okas_ana_kod <> '' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
)
  AND c.idare_id = (
    SELECT idare_id FROM ekap_contract
    WHERE idare_id <> '' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
)
  AND c.sozlesme_tarihi >= now() - interval '5 years';

\echo ''
\echo '=== 7. Tekrar eden seri tespiti — seri_anahtar GROUP BY ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT seri_anahtar, idare_id, count(*) AS n, min(ilan_tarihi), max(ilan_tarihi)
FROM ekap_tender
WHERE seri_anahtar <> ''
GROUP BY seri_anahtar, idare_id
HAVING count(*) >= 3
LIMIT 50;

\echo ''
\echo '=== 8. Mevcut indeksler (Tender + Contract) ==='
SELECT relname AS tablo, indexrelname AS indeks,
       pg_size_pretty(pg_relation_size(indexrelid)) AS boyut,
       idx_scan AS kullanim
FROM pg_stat_user_indexes
WHERE relname IN ('ekap_tender','ekap_contract')
ORDER BY relname, pg_relation_size(indexrelid) DESC;
