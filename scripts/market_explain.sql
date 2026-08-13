-- ═══════════════════════════════════════════════════════════════════════
--  Pazar panosu (Adım 6) — MATERIALIZE KARARI ÖNCESİ ölçüm
--
--  Kullanım:
--    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
--      < scripts/market_explain.sql
--
--  ⚠️ Plan "materialize, kesin" diyordu ama o karar veri dolmadan, ölçüm yapılamadan
--     verilmişti. `MarketStat` bedava değil: yenileme görevi, gece penceresi,
--     toplanabilirlik kuralları (medyan ve distinct sayılar TOPLANAMAZ), bayatlık
--     riski ve bakım yükü. Canlı sorgular yeterince hızlıysa yazmamak doğrusu.
--
--  KARAR EŞİĞİ: aşağıdaki panel sorguları **< 200 ms** ise materialize etme.
--               Biri belirgin yavaşsa yalnızca onu materialize et (hepsini değil).
--
--  Bağlam: 1.407.379 sözleşme / 1 GB · grain (yil,bucket,il,tip) = 143.105 kombinasyon
--          1.167 bucket · 81 il · 4 tür · 2010-2026 · 214.079 satırda bucket BOŞ
--          Tam GROUP BY (yenileme görevinin yapacağı iş) = 3.627 ms
-- ═══════════════════════════════════════════════════════════════════════

\timing on

-- Test değerleri önceden çözülür (ölçüme dahil değil; bkz. pro_explain.sql dersi).
SELECT okas_bucket AS bucket FROM ekap_contract
 WHERE okas_bucket <> '' AND sozlesme_tarihi >= '2024-01-01'
 GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
\gset
\echo 'Test bucket:' :bucket

\echo ''
\echo '=== A. PANO ANA EKRANI: bir yilda en cok harcama yapilan 20 is grubu ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT okas_bucket,
       count(*) AS adet,
       sum(sozlesme_bedeli_num) AS toplam,
       sum(indirim_orani) AS ind_toplam,
       count(indirim_orani) AS ind_ornek
FROM ekap_contract
WHERE sozlesme_tarihi >= '2025-01-01' AND sozlesme_tarihi < '2026-01-01'
  AND okas_bucket <> ''
GROUP BY 1
ORDER BY toplam DESC NULLS LAST
LIMIT 20;

\echo ''
\echo '=== B. DRILL-DOWN: tek is grubunun yillara gore seyri ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT EXTRACT(YEAR FROM sozlesme_tarihi)::int AS yil,
       count(*) AS adet,
       sum(sozlesme_bedeli_num) AS toplam,
       sum(indirim_orani) AS ind_toplam,
       count(indirim_orani) AS ind_ornek
FROM ekap_contract
WHERE okas_bucket = :'bucket'
GROUP BY 1
ORDER BY 1;

\echo ''
\echo '=== C. DRILL-DOWN: bir yil + is grubunda il kirilimi ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT il_id, count(*) AS adet, sum(sozlesme_bedeli_num) AS toplam
FROM ekap_contract
WHERE okas_bucket = :'bucket'
  AND sozlesme_tarihi >= '2025-01-01' AND sozlesme_tarihi < '2026-01-01'
GROUP BY 1
ORDER BY toplam DESC NULLS LAST
LIMIT 20;

\echo ''
\echo '=== D. DRILL-DOWN: bir yil + is grubunda en cok is alan firmalar ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT yuklenici_id, count(*) AS adet, sum(sozlesme_bedeli_num) AS toplam
FROM ekap_contract
WHERE okas_bucket = :'bucket'
  AND sozlesme_tarihi >= '2025-01-01' AND sozlesme_tarihi < '2026-01-01'
  AND yuklenici_id IS NOT NULL
GROUP BY 1
ORDER BY toplam DESC NULLS LAST
LIMIT 20;

\echo ''
\echo '=== E. OZET KART: bir yilin toplam gorunumu ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS sozlesme,
       count(DISTINCT yuklenici_id) AS firma,
       sum(sozlesme_bedeli_num) AS toplam,
       sum(indirim_orani) AS ind_toplam,
       count(indirim_orani) AS ind_ornek
FROM ekap_contract
WHERE sozlesme_tarihi >= '2025-01-01' AND sozlesme_tarihi < '2026-01-01';

\echo ''
\echo '=== F. IL PANOSU: bir yilda bir ildeki is gruplari ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT okas_bucket, count(*) AS adet, sum(sozlesme_bedeli_num) AS toplam
FROM ekap_contract
WHERE il_id = 6
  AND sozlesme_tarihi >= '2025-01-01' AND sozlesme_tarihi < '2026-01-01'
  AND okas_bucket <> ''
GROUP BY 1
ORDER BY toplam DESC NULLS LAST
LIMIT 20;
