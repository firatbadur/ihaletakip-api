-- ═══════════════════════════════════════════════════════════════════════
--  Ölü indeks denetimi — DROP kararı ÖNCESİ kanıt
--
--  Kullanım:
--    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
--      < scripts/index_audit.sql
--
--  ⚠️ **`idx_scan = 0` TEK BAŞINA yeterli kanıt DEĞİLDİR:**
--    1. Sayaçlar `pg_stat_reset()` ile ya da (bazı durumlarda) sunucu çökmesiyle
--       sıfırlanır → "hiç kullanılmadı" değil "sayaç sıfırlandığından beri
--       kullanılmadı" demektir. Aşağıdaki 1. sorgu sayacın yaşını gösterir.
--    2. UNIQUE indeksler kısıt uygular; `idx_scan=0` olsa da DÜŞÜRÜLEMEZ.
--    3. Yabancı anahtar hedefleri silme/güncelleme kontrollerinde kullanılır.
--    4. Yılda birkaç kez koşan bir rapor sorgusunun indeksi de 0 görünebilir.
--  Bu yüzden 3. sorgu düşürülebilir adayları **kısıt/FK olmayanlarla** sınırlar.
-- ═══════════════════════════════════════════════════════════════════════

\echo '=== 1. Sayaclar ne zamandir topluyor? (yeni sifirlandiysa 0 degerleri anlamsiz) ==='
SELECT stats_reset, now() - stats_reset AS sayac_yasi
FROM pg_stat_database
WHERE datname = current_database();

\echo ''
\echo '=== 2. Bu tablolardaki TUM indeksler, kullanim ve boyutlariyla ==='
SELECT s.relname AS tablo,
       s.indexrelname AS indeks,
       s.idx_scan AS taramalar,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS boyut,
       i.indisunique AS unique_mi,
       i.indisprimary AS pk_mi
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.relname IN ('ekap_tender', 'ekap_contract', 'ekap_okasitem', 'ekap_contractor')
ORDER BY s.idx_scan ASC, pg_relation_size(s.indexrelid) DESC;

\echo ''
\echo '=== 3. DUSURULEBILIR ADAYLAR: kullanilmayan + kisit degil + FK hedefi degil ==='
SELECT s.relname AS tablo,
       s.indexrelname AS indeks,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS boyut,
       pg_get_indexdef(s.indexrelid) AS tanim
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.relname IN ('ekap_tender', 'ekap_contract', 'ekap_okasitem', 'ekap_contractor')
  AND s.idx_scan = 0
  AND NOT i.indisunique
  AND NOT i.indisprimary
  -- FK kısıtının dayandığı indeksleri dışla
  AND NOT EXISTS (
      SELECT 1 FROM pg_constraint c
      WHERE c.conindid = s.indexrelid
  )
ORDER BY pg_relation_size(s.indexrelid) DESC;

\echo ''
\echo '=== 4. Toplam geri kazanilabilir alan ==='
SELECT pg_size_pretty(sum(pg_relation_size(s.indexrelid))) AS kazanilacak,
       count(*) AS indeks_sayisi
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.relname IN ('ekap_tender', 'ekap_contract', 'ekap_okasitem', 'ekap_contractor')
  AND s.idx_scan = 0
  AND NOT i.indisunique
  AND NOT i.indisprimary
  AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = s.indexrelid);
