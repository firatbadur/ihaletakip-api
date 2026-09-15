#!/usr/bin/env bash
# ============================================================================
# ihaletakip — veritabanını ESKİ prod sunucusundan (91.241.49.109) tinyfect
# sunucusuna (173.249.43.236) taşır. PROD sunucusunda root olarak çalışır.
# ============================================================================
#
# Kullanım:
#   scripts/db_tasima.sh on-kontrol        # gündüz de güvenle çalışır, hiçbir şey KOPYALAMAZ
#   scripts/db_tasima.sh zamanla 00:30     # verilen TR saatine (systemd) kurar
#   scripts/db_tasima.sh calistir          # asıl iş — elle başlatmak için (tmux içinde!)
#   scripts/db_tasima.sh dogrula           # kopyayı yeniden almadan yalnızca geri yükle + doğrula
#   scripts/db_tasima.sh durum             # zamanlayıcı / son log / slot / worker durumu
#   scripts/db_tasima.sh iptal             # zamanlayıcıyı kaldırır, çalışıyorsa temizce durdurur
#
# Ne yapar (calistir):
#   1. Ön kontrol (hepsi geçmezse HİÇBİR ŞEY yapmaz)
#   2. beat + celery worker'larını duraklatır (WORKERLARI_DURAKLAT=1)
#   3. Replikasyon slotu açar, pg_basebackup'ı SSH ile doğrudan hedefe akıtır
#   4. Slotu siler, worker'ları geri açar (prod tarafı biter)
#   5. Hedefte: tar'ı Docker volume'una açar → pg_verifybackup → geçici (ağsız)
#      bir Postgres ile açar → tablo karşılaştırması → pg_amcheck
#   6. Geçici konteyneri kaldırır; volume + base.tar kalır
#
# ⚠️⚠️ BU BİR KESME (cutover) DEĞİLDİR. Web açık kalır; yedek bittikten SONRA prod'a
#  yazılan veri kopyada yoktur. Çıktı: (a) sunucu dışı tam yedek, (b) taşımanın taban
#  noktası. Uygulama hâlâ eski sunucudan çalışır.
#
# ⚠️⚠️ NEDEN pg_dump DEĞİL pg_basebackup (fiziksel kopya):
#  Prod diski ~1,5 MB/s'e kısıtlı (ölçüldü 2026-09-15; bkz. CLAUDE.md "Üretim
#  donanımı"). pg_dump TOAST'ı (12,7 GB) büyük ölçüde rastgele erişimle okur VE
#  döküm boyunca tek bir transaction/snapshot'ı saatlerce açık tutar → vacuum
#  temizliği gece boyu bloklanır. pg_basebackup dosyaları sıralı okur, transaction
#  açmaz, hedefte indeks yeniden kurulmaz.
#  ⚠️ Bedeli: hedef BİREBİR aynı PG ana sürümü ve aynı libc olmalı → imaj digest'i
#  çalışma anında prod konteynerinden okunur ve hedefte DİGEST ile çekilir. Farklı
#  libc/collation, metin indekslerini (trigram GIN dahil) SESSİZCE bozar.
#
# ⚠️⚠️ NEDEN REPLİKASYON SLOTU:
#  Prod'da `wal_keep_size=0` (ölçüldü). `-X fetch` WAL'ı yedeğin SONUNDA toplar;
#  slot olmasa saatler süren kopyanın başındaki WAL çoktan geri dönüştürülmüş olur
#  ve iş EN SONDA "requested WAL segment has already been removed" ile düşerdi.
#  Slot yedekten ÖNCE `reserve_wal=true` ile açılır. Üretim ~61 MB WAL/sa (ölçüldü)
#  → gecelik tutma birkaç yüz MB. Slot trap'te HER durumda silinir (SLOTU_KORU=1
#  hariç). ⚠️ Unutulan slot WAL'ı süresiz biriktirir ve sonunda diski doldurur →
#  bekçi, prod boş diski KRITIK_PROD_BOS_GB altına inerse yedeği durdurur.
#
# ⚠️ YALNIZCA ÇEKİRDEK `docker` KOMUTLARI — `docker compose` KULLANILMAZ.
#  Disk doygunken compose eklentisinin (~60 MB binary) yüklenmesi zaman aşımına
#  düşüp "unknown shorthand flag: 'T' in -T" veriyor (üretimde görüldü 2026-09-15).
#
# ⚠️ ZAMAN AŞIMLARI KONTEYNER İÇİNDEN yönetilir: dışarıdaki `docker exec` istemcisini
#  öldürmek içerideki pg_basebackup'ı ÖLDÜRMEZ (2026-09-14 yetim sorgu dersi) →
#  temizlik walsender'ı sonlandırır + konteyner içinde pkill yapar.
#
# ⚠️ NEDEN beat DE duraklatılır (yalnızca worker'lar değil): beat çalışırken worker
#  yoksa görevler Redis'te birikir; worker'lar dönünce `ekap.mobil.tasks.tik`
#  dahil yüzlerce görev ART ARDA koşar. EKAP mobil hız sınırı IP tabanlı ve cezası
#  büyüyor (CLAUDE.md) → biriken sürüyü hiç oluşturmamak gerekir.
#  Sabah bildirimleri (07:00 TR) kaçmasın diye worker'lar WORKER_DONUS_TR'de
#  (vars. 06:30) kopya sürse bile geri açılır.
# ============================================================================

set -Eeuo pipefail

# ── Ayarlar (ortam değişkeniyle ezilebilir) ─────────────────────────────────────
DB_KONTEYNER="${DB_KONTEYNER:-ihaletakip-api-db-1}"
DB_KULLANICI="${DB_KULLANICI:-ihale}"
DB_ADI="${DB_ADI:-ihaletakip}"
HEDEF="${HEDEF:-tinyfect-tasima}"                       # /root/.ssh/config takma adı
HEDEF_DIZIN="${HEDEF_DIZIN:-/root/ihaletakip-tasima}"
HEDEF_VOLUME="${HEDEF_VOLUME:-ihaletakip-api_pgdata}"   # compose'un bekleyeceği ad
DOGRULA_KONTEYNER="${DOGRULA_KONTEYNER:-ihaletakip-tasima-dogrula}"
SLOT="${SLOT:-ihaletakip_tasima}"
MAX_RATE="${MAX_RATE:-}"                    # pg_basebackup -r (örn. 1M); boş = sınırsız
WORKERLARI_DURAKLAT="${WORKERLARI_DURAKLAT:-1}"
WORKER_DONUS_TR="${WORKER_DONUS_TR:-06:30}"
SON_SAAT_TR="${SON_SAAT_TR:-}"              # doluysa: bu saatte bitmemiş yedek DURDURULUR
AMCHECK="${AMCHECK:-1}"
ZORLA="${ZORLA:-0}"                         # 1 = dolu hedef volume'u sil ve yeniden yaz
SLOTU_KORU="${SLOTU_KORU:-0}"               # 1 = başarıda slotu silme (replika tabanlı kesme için)
MIN_PROD_BOS_GB="${MIN_PROD_BOS_GB:-10}"
KRITIK_PROD_BOS_GB="${KRITIK_PROD_BOS_GB:-5}"
CALISMA_DIZINI="${CALISMA_DIZINI:-/root/db_tasima}"

TIMER_ADI="ihaletakip-db-tasima"
DURAKLATILANLAR="$CALISMA_DIZINI/duraklatilanlar"
# Küçük komutlar tek SSH bağlantısını paylaşır (doğrulama yoklaması yüzlerce çağrı
# yapar); saatler süren aktarım ise AYRI bağlantı kullanır — ana bağlantı düşerse
# yedek de düşmesin.
SSH_KUCUK=(-o ControlMaster=auto -o "ControlPath=/root/.ssh/cm-tasima-%r@%h:%p" -o ControlPersist=10m)
SSH_AKIS=(-o ControlMaster=no -o ControlPath=none -o ServerAliveInterval=30 -o ServerAliveCountMax=20)

BASLANGIC=$(date +%s)
PG_IMAJ=""
REFERANS_SQL=""
SLOT_ACIK=0
YEDEK_SURUYOR=0
BEKCI_PID=""

# ── Yardımcılar ─────────────────────────────────────────────────────────────────
log() { printf '[%s TR] %s\n' "$(TZ=Europe/Istanbul date '+%F %T')" "$*"; }
die() { log "✗ HATA: $*"; exit 1; }
sure_bicim() { printf '%d sa %02d dk' $(( $1 / 3600 )) $(( $1 % 3600 / 60 )); }

# SQL stdin'den verilir → SSH/docker katmanlarında tırnak sorunu olmaz.
pgq() {
  printf '%s\n' "$1" | docker exec -i "$DB_KONTEYNER" \
    psql -U "$DB_KULLANICI" -d "$DB_ADI" -XtAq -v ON_ERROR_STOP=1 -f -
}
# ⚠️ -n: stdin'i OKUMAZ. Betik `bash -s < betik` ile beslenirse, stdin okuyan bir
#  ssh betiğin GERİ KALANINI yutar ve sessizce durur (prova sırasında yaşandı).
hedef() { ssh -n "${SSH_KUCUK[@]}" "$HEDEF" "$@"; }
hpgq() {
  printf '%s\n' "$1" | ssh "${SSH_KUCUK[@]}" "$HEDEF" \
    "docker exec -i $DOGRULA_KONTEYNER psql -U $DB_KULLANICI -d $DB_ADI -XtAq -v ON_ERROR_STOP=1 -f -"
}

pg_imaj() {
  docker image inspect -f '{{index .RepoDigests 0}}' \
    "$(docker inspect -f '{{.Image}}' "$DB_KONTEYNER")"
}
prod_bos_gb() { df -BG --output=avail / | tail -1 | tr -dc 0-9; }
hedef_bos_gb() { hedef "df -BG --output=avail / | tail -1 | tr -dc 0-9"; }
db_boyut_gb() { pgq "SELECT ceil(pg_database_size(current_database()) / 1073741824.0)::int"; }

# Verilen TR saatinin (HH:MM) şu andan sonraki ilk gerçekleşmesi (epoch).
sonraki_tr_epoch() {
  local t
  t=$(TZ=Europe/Istanbul date -d "$1" +%s)
  if (( t <= $(date +%s) )); then t=$(TZ=Europe/Istanbul date -d "tomorrow $1" +%s); fi
  echo "$t"
}

hedef_volume_durumu() {
  if ! hedef "docker volume inspect '$HEDEF_VOLUME' >/dev/null 2>&1"; then echo yok; return; fi
  if hedef "docker run --rm --network none -v '$HEDEF_VOLUME':/d '$PG_IMAJ' sh -c '[ -z \"\$(ls -A /d)\" ]'"; then
    echo bos
  else
    echo dolu
  fi
}

# beat ÖNCE listelenir (durdurma sırası), açarken tersine çevrilir.
worker_konteynerleri() {
  local tum
  tum=$(docker ps --format '{{.Names}}' | grep -E '^ihaletakip-api-.*(worker|beat)-[0-9]+$' || true)
  { grep -- '-beat-' <<<"$tum" || true; grep -v -- '-beat-' <<<"$tum" || true; } | sed '/^$/d'
}

workerlari_duraklat() {
  : > "$DURAKLATILANLAR"
  local c
  for c in $(worker_konteynerleri); do
    # -t 120: süre bütçeli görevler (90-270 sn) temiz bitsin
    if docker stop -t 120 "$c" >/dev/null; then
      echo "$c" >> "$DURAKLATILANLAR"
      log "  durduruldu: $c"
    fi
  done
}

workerlari_ac() {
  [ -s "$DURAKLATILANLAR" ] || return 0
  local c
  # önce worker'lar, EN SON beat (worker yokken görev biriktirmesin)
  for c in $(grep -v -- '-beat-' "$DURAKLATILANLAR" || true) $(grep -- '-beat-' "$DURAKLATILANLAR" || true); do
    if docker start "$c" >/dev/null 2>&1; then
      log "  başlatıldı: $c"
    else
      log "  ✗ BAŞLATILAMADI: $c — elle: docker start $c"
    fi
  done
  : > "$DURAKLATILANLAR"
}

basebackup_durdur() {
  pgq "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'pg_basebackup'" \
    >/dev/null 2>&1 || true
  docker exec "$DB_KONTEYNER" pkill -f pg_basebackup >/dev/null 2>&1 || true
}

slot_sil() {
  local i
  for i in 1 2 3 4 5 6; do
    if [ "$(pgq "SELECT count(*) FROM pg_replication_slots WHERE slot_name = '$SLOT'" 2>/dev/null)" = 0 ]; then
      [ "$SLOT_ACIK" = 1 ] && log "slot '$SLOT' silindi"
      SLOT_ACIK=0
      return 0
    fi
    # aktif slot silinemez; walsender az önce sonlandırıldıysa birkaç saniye sürer
    pgq "SELECT pg_drop_replication_slot('$SLOT') FROM pg_replication_slots
          WHERE slot_name = '$SLOT' AND NOT active" >/dev/null 2>&1 || true
    [ "$i" -gt 1 ] && sleep 5
  done
  if [ "$(pgq "SELECT count(*) FROM pg_replication_slots WHERE slot_name = '$SLOT'" 2>/dev/null)" = 0 ]; then
    log "slot '$SLOT' silindi"
    SLOT_ACIK=0
    return 0
  fi
  log "✗ UYARI: slot '$SLOT' SİLİNEMEDİ — WAL birikir, ELLE silin:"
  log "    docker exec $DB_KONTEYNER psql -U $DB_KULLANICI -d $DB_ADI -c \"SELECT pg_drop_replication_slot('$SLOT')\""
}

# ── Bekçi (arka plan) ───────────────────────────────────────────────────────────
bekci() {
  set +e
  trap - ERR
  local son_ilerleme=0 donuldu=0 pb p
  local donus_epoch son_epoch=""
  donus_epoch=$(sonraki_tr_epoch "$WORKER_DONUS_TR")
  [ -n "$SON_SAAT_TR" ] && son_epoch=$(sonraki_tr_epoch "$SON_SAAT_TR")
  while true; do
    sleep 60
    pb=$(prod_bos_gb 2>/dev/null || echo 999)
    if [ "${pb:-999}" -lt "$KRITIK_PROD_BOS_GB" ]; then
      log "✗ BEKÇİ: prod boş disk ${pb} GB < ${KRITIK_PROD_BOS_GB} GB — yedek DURDURULUYOR"
      basebackup_durdur
    fi
    if [ -n "$son_epoch" ] && (( $(date +%s) >= son_epoch )); then
      log "✗ BEKÇİ: SON_SAAT_TR=$SON_SAAT_TR geçti — gündüz trafiğini boğmamak için yedek DURDURULUYOR"
      basebackup_durdur
      son_epoch=""
    fi
    if [ "$donuldu" = 0 ] && [ -s "$DURAKLATILANLAR" ] && (( $(date +%s) >= donus_epoch )); then
      log "! BEKÇİ: $WORKER_DONUS_TR TR — sabah bildirimleri kaçmasın, worker'lar açılıyor (kopya sürüyor)"
      workerlari_ac
      donuldu=1
    fi
    if (( $(date +%s) - son_ilerleme >= 300 )); then
      son_ilerleme=$(date +%s)
      p=$(pgq "SELECT phase || ' | ' || pg_size_pretty(backup_streamed) || ' / '
                     || coalesce(pg_size_pretty(backup_total), '?') || ' | %'
                     || coalesce(round(100.0 * backup_streamed / nullif(backup_total, 0), 1)::text, '?')
                FROM pg_stat_progress_basebackup" 2>/dev/null)
      [ -n "$p" ] && log "  ilerleme: $p | prod boş ${pb} GB | yük $(cut -d' ' -f1 /proc/loadavg)"
    fi
  done
}

# ── Temizlik (her çıkışta) ──────────────────────────────────────────────────────
temizlik() {
  local rc=$?
  set +e
  trap - ERR
  # ⚠️⚠️ Temizlik yarıda KESİLEMEZ olmalı. systemd durdururken (`iptal`) SIGTERM'i tee
  #  DAHİL tüm süreçlere yollar; tee ölünce ilk `log` kırık boruya yazar ve SIGPIPE
  #  betiği temizliğin ortasında öldürür → slot SİLİNMEZ, worker'lar KAPALI kalır.
  #  Prova sırasında bulundu (2026-09-15): log'da "SONUÇ" satırı yoktu.
  #  Çözüm: SIGPIPE/INT/TERM yok say + çıktıyı tee'yi atlayıp doğrudan dosyaya yaz.
  trap '' PIPE INT TERM
  [ -n "${LOG:-}" ] && exec >>"$LOG" 2>&1
  [ -n "$BEKCI_PID" ] && kill "$BEKCI_PID" 2>/dev/null
  if [ "$YEDEK_SURUYOR" = 1 ]; then
    log "yarım kalan pg_basebackup durduruluyor"
    basebackup_durdur
  fi
  if [ "$SLOT_ACIK" = 1 ]; then
    if [ "$SLOTU_KORU" = 1 ] && [ "$rc" = 0 ]; then
      log "! slot '$SLOT' KORUNDU (SLOTU_KORU=1) — WAL biriktirir (~61 MB/sa). Silmek için:"
      log "    docker exec $DB_KONTEYNER psql -U $DB_KULLANICI -d $DB_ADI -c \"SELECT pg_drop_replication_slot('$SLOT')\""
    else
      slot_sil
    fi
  fi
  workerlari_ac
  hedef "docker rm -f $DOGRULA_KONTEYNER >/dev/null 2>&1" 2>/dev/null
  local sure durum
  sure=$(( $(date +%s) - BASLANGIC ))
  if [ "$rc" = 0 ]; then durum="BAŞARILI"; else durum="BAŞARISIZ (çıkış kodu $rc)"; fi
  printf '%s TR | %s | süre %s | log %s\n' "$(TZ=Europe/Istanbul date '+%F %T')" "$durum" \
    "$(sure_bicim "$sure")" "${LOG:-?}" > "$CALISMA_DIZINI/son-durum.txt"
  log "SONUÇ: $durum — toplam $(sure_bicim "$sure")"
}

# ── Ön kontrol ──────────────────────────────────────────────────────────────────
on_kontrol() {
  set +e
  local hata=0 v boyut pb hb gerek w
  ok()  { log "  ✓ $*"; }
  bad() { log "  ✗ $*"; hata=1; }

  log "ÖN KONTROL — hiçbir şey kopyalanmaz"
  # ⚠️ tzdata yoksa TZ=Europe/Istanbul SESSİZCE UTC'ye düşer → 06:30 dönüşü 3 saat kayar
  if [ "$(TZ=Europe/Istanbul date +%z)" = "+0300" ]; then
    ok "saat dilimi verisi (Europe/Istanbul = +0300)"
  else
    bad "Europe/Istanbul çözülemiyor (tzdata?) — zamanlar UTC'ye kayar"
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$DB_KONTEYNER" 2>/dev/null)" != true ]; then
    bad "DB konteyneri çalışmıyor: $DB_KONTEYNER"; set -e; return 1
  fi
  ok "DB konteyneri çalışıyor: $DB_KONTEYNER"
  docker exec "$DB_KONTEYNER" pg_isready -U "$DB_KULLANICI" -q && ok "pg_isready" || bad "pg_isready başarısız"

  v=$(pgq "SELECT rolsuper OR rolreplication FROM pg_roles WHERE rolname = current_user")
  [ "$v" = t ] && ok "rol replikasyon yetkili" || bad "'$DB_KULLANICI' rolünde REPLICATION yetkisi yok"
  v=$(pgq "SHOW wal_level")
  [ -n "$v" ] && [ "$v" != minimal ] && ok "wal_level=$v" || bad "wal_level='$v' (replica olmalı)"

  if docker exec "$DB_KONTEYNER" psql "user=$DB_KULLANICI replication=true" -XtAqc "IDENTIFY_SYSTEM;" >/dev/null 2>&1; then
    ok "replikasyon bağlantısı (yerel soket — pg_hba değişikliği gerekmez)"
  else
    bad "replikasyon bağlantısı reddedildi (pg_hba?)"
  fi
  if pgq "SELECT pg_create_physical_replication_slot('${SLOT}_test', false)" >/dev/null &&
     pgq "SELECT pg_drop_replication_slot('${SLOT}_test')" >/dev/null; then
    ok "slot aç/sil testi"
  else
    bad "test slotu açılamadı/silinemedi"
    pgq "SELECT pg_drop_replication_slot('${SLOT}_test') FROM pg_replication_slots WHERE slot_name = '${SLOT}_test'" >/dev/null 2>&1
  fi
  v=$(pgq "SELECT count(*) FROM pg_replication_slots WHERE slot_name = '$SLOT'")
  [ "$v" = 0 ] && ok "artık '$SLOT' slotu yok" || log "  ! '$SLOT' slotu zaten var — calistir önce onu silecek"

  pb=$(prod_bos_gb)
  [ "${pb:-0}" -ge "$MIN_PROD_BOS_GB" ] && ok "prod boş disk ${pb} GB" || bad "prod boş disk ${pb} GB < ${MIN_PROD_BOS_GB} GB"
  PG_IMAJ=$(pg_imaj 2>/dev/null)
  [[ "$PG_IMAJ" == postgres@sha256:* ]] && ok "PG imajı: $PG_IMAJ" || bad "PG imaj digest'i okunamadı ('$PG_IMAJ')"
  boyut=$(db_boyut_gb)
  ok "DB boyutu ~${boyut} GB"

  if ! hedef true 2>/dev/null; then
    bad "SSH → $HEDEF başarısız"; set -e; return 1
  fi
  ok "SSH → $HEDEF ($(hedef hostname))"
  hedef "docker info >/dev/null 2>&1" && ok "hedefte docker çalışıyor" || bad "hedefte docker yok"
  hb=$(hedef_bos_gb)
  gerek=$(( ${boyut:-30} * 5 / 2 + 10 ))   # base.tar + açılmış volume + pay
  [ "${hb:-0}" -ge "$gerek" ] && ok "hedef boş disk ${hb} GB (gereken ~${gerek} GB)" || bad "hedef boş disk ${hb} GB < ${gerek} GB"
  if [ -n "$PG_IMAJ" ] && hedef "docker pull -q '$PG_IMAJ' >/dev/null"; then
    ok "hedefte birebir aynı imaj (digest) çekildi"
  else
    bad "hedefte imaj çekilemedi"
  fi
  case "$(hedef_volume_durumu)" in
    yok)  ok "hedef volume '$HEDEF_VOLUME' yok (oluşturulacak)" ;;
    bos)  ok "hedef volume '$HEDEF_VOLUME' boş" ;;
    dolu) if [ "$ZORLA" = 1 ]; then
            log "  ! hedef volume DOLU — ZORLA=1: silinip yeniden yazılacak"
          else
            bad "hedef volume '$HEDEF_VOLUME' DOLU (üzerine yazmak için ZORLA=1)"
          fi ;;
  esac
  if hedef "docker ps -a --format '{{.Names}}' | grep -qx '$DOGRULA_KONTEYNER'"; then
    log "  ! hedefte eski '$DOGRULA_KONTEYNER' konteyneri var — calistir kaldıracak"
  fi

  w=$(worker_konteynerleri | tr '\n' ' ')
  ok "duraklatılacak (WORKERLARI_DURAKLAT=$WORKERLARI_DURAKLAT): ${w:-yok}"
  command -v systemd-run >/dev/null && ok "systemd-run mevcut" || bad "systemd-run yok"

  if [ "$hata" = 0 ]; then log "ÖN KONTROL: HEPSİ TAMAM"; else log "ÖN KONTROL: SORUN VAR"; fi
  set -e
  return "$hata"
}

# ── Prod referans değerleri ─────────────────────────────────────────────────────
referans_al() {
  log "prod referans değerleri alınıyor (küçük tablo: tam sayım, büyük: max(id))"
  local satirlar t kucuk idvar adet mx
  local parca=()
  # Karar prod kataloğundan verilir; AYNI SQL hedefte de koşar → iki taraf aynı
  # tabloları aynı yöntemle ölçer. Büyük tablolarda count(*) prod diskinde saatler
  # sürerdi; max(id) PK indeksinin sağ kenarından okunur (birkaç sayfa).
  satirlar=$(pgq "SELECT c.relname || '|' || (pg_table_size(c.oid) < 20 * 1024 * 1024)::int || '|'
                  || EXISTS (SELECT 1 FROM information_schema.columns k
                              WHERE k.table_schema = 'public' AND k.table_name = c.relname
                                AND k.column_name = 'id' AND k.data_type IN ('integer', 'bigint', 'smallint'))::int
                    FROM pg_class c
                   WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
                   ORDER BY 1")
  while IFS='|' read -r t kucuk idvar; do
    [ -n "$t" ] || continue
    adet="NULL"; mx="NULL"
    [ "$kucuk" = 1 ] && adet="(SELECT count(*) FROM public.\"$t\")"
    [ "$idvar" = 1 ] && mx="(SELECT max(id) FROM public.\"$t\")"
    parca+=("SELECT '$t' AS t, $adet AS adet, $mx AS mx")
  done <<<"$satirlar"
  [ "${#parca[@]}" -gt 0 ] || die "prod'da tablo listesi alınamadı"
  REFERANS_SQL="SELECT t || '|' || coalesce(adet::text, '-') || '|' || coalesce(mx::text, '-') FROM ("
  REFERANS_SQL+=$(printf '%s\nUNION ALL\n' "${parca[@]}" | sed '$d')
  REFERANS_SQL+=") x ORDER BY t;"
  printf '%s\n' "$REFERANS_SQL" > "$CALISMA_DIZINI/referans.sql"
  pgq "$REFERANS_SQL" > "$CALISMA_DIZINI/referans-prod.txt"
  log "  $(wc -l < "$CALISMA_DIZINI/referans-prod.txt") tablo kaydedildi"
}

# ── Hedefte geri yükleme + doğrulama ────────────────────────────────────────────
geri_yukle_ve_dogrula() {
  local i hazir=0 sorun

  log "hedefte açılıyor → volume '$HEDEF_VOLUME'"
  hedef "docker volume create '$HEDEF_VOLUME' >/dev/null && docker run --rm --network none \
           -v '$HEDEF_VOLUME':/var/lib/postgresql/data -v '$HEDEF_DIZIN':/yedek:ro '$PG_IMAJ' \
           sh -c 'cd /var/lib/postgresql/data && tar -xf /yedek/base.tar && chown -R postgres:postgres . && chmod 700 .'" \
    || die "base.tar açılamadı"
  log "✓ açıldı"

  # ⚠️ pg_verifybackup Postgres BAŞLAMADAN önce koşmalı: başlatmak dosyaları değiştirir
  if hedef "docker run --rm --network none -v '$HEDEF_VOLUME':/v:ro '$PG_IMAJ' test -f /v/backup_manifest"; then
    log "pg_verifybackup (dosya sağlama toplamları + WAL zinciri)"
    hedef "docker run --rm --network none --user postgres -v '$HEDEF_VOLUME':/var/lib/postgresql/data \
             '$PG_IMAJ' pg_verifybackup /var/lib/postgresql/data" || die "pg_verifybackup BAŞARISIZ — kopya bozuk"
    log "✓ pg_verifybackup temiz"
  else
    log "! backup_manifest yok — pg_verifybackup atlandı (bütünlük pg_amcheck ile denetlenecek)"
  fi

  # ⚠️ --network none: tinyfect'in kendi yığınıyla hiçbir bağlantı kurulamaz, port açılmaz.
  # ⚠️ --memory: aynı makinedeki tinyfect ve SQL Server aç kalmasın.
  log "doğrulama sunucusu başlatılıyor (ağsız, portsuz, bellek sınırlı)"
  hedef "docker rm -f $DOGRULA_KONTEYNER >/dev/null 2>&1; docker run -d --name $DOGRULA_KONTEYNER \
           --network none --memory 24g -v '$HEDEF_VOLUME':/var/lib/postgresql/data '$PG_IMAJ' \
           postgres -c shared_buffers=8GB -c max_connections=100 -c max_worker_processes=16 \
                    -c max_parallel_workers=8 -c max_parallel_maintenance_workers=4 >/dev/null" \
    || die "doğrulama konteyneri başlatılamadı"
  for i in $(seq 1 360); do
    if [ "$(hedef "docker inspect -f '{{.State.Running}}' $DOGRULA_KONTEYNER" 2>/dev/null)" != true ]; then
      hedef "docker logs --tail 40 $DOGRULA_KONTEYNER" 2>&1 | sed 's/^/    /'
      die "doğrulama sunucusu çöktü (yukarıdaki log)"
    fi
    if hedef "docker exec $DOGRULA_KONTEYNER pg_isready -U $DB_KULLANICI -q" 2>/dev/null; then
      hazir=1; break
    fi
    sleep 5
  done
  [ "$hazir" = 1 ] || die "doğrulama sunucusu 30 dk içinde hazır olmadı"
  [ "$(hpgq 'SELECT pg_is_in_recovery()')" = f ] || die "kurtarma tamamlanmadı (hâlâ recovery modunda)"
  log "✓ kopya açıldı, kurtarma tamam — boyut $(hpgq 'SELECT pg_size_pretty(pg_database_size(current_database()))')"

  log "tablo karşılaştırması (prod referansı yedek bittikten hemen sonra alındı → küçük farklar normaldir)"
  hpgq "$REFERANS_SQL" > "$CALISMA_DIZINI/referans-hedef.txt" || die "hedefte karşılaştırma sorgusu çalışmadı"
  awk -F'|' '
    function yakin(a, b,   d, t) {
      if (a == "-" && b == "-") return 1
      if (a == "-" || b == "-") return 0
      d = a - b; if (d < 0) d = -d
      t = (a > b ? a : b) * 0.005; if (t < 500) t = 500
      return d <= t
    }
    NR == FNR { p[$1] = $2 "|" $3; next }
    { h[$1] = $2 "|" $3 }
    END {
      for (t in p) {
        if (!(t in h)) { printf "  ✗ %-34s HEDEFTE YOK\n", t; continue }
        split(p[t], a, "|"); split(h[t], b, "|")
        if (yakin(a[1], b[1]) && yakin(a[2], b[2]))
          isaret = (a[1] == b[1] && a[2] == b[2]) ? "✓" : "~"
        else
          isaret = "✗"
        printf "  %s %-34s adet prod=%s hedef=%s | max(id) prod=%s hedef=%s\n", isaret, t, a[1], b[1], a[2], b[2]
      }
    }' "$CALISMA_DIZINI/referans-prod.txt" "$CALISMA_DIZINI/referans-hedef.txt" \
    | sort -k2 > "$CALISMA_DIZINI/karsilastirma.txt"
  cat "$CALISMA_DIZINI/karsilastirma.txt"
  sorun=$(grep -c '✗' "$CALISMA_DIZINI/karsilastirma.txt" || true)
  [ "$sorun" = 0 ] || die "$sorun tabloda uyuşmazlık"
  log "✓ $(wc -l < "$CALISMA_DIZINI/karsilastirma.txt") tablo tutarlı"

  if [ "$AMCHECK" = 1 ]; then
    # ⚠️ --install-missing kopyada `amcheck` eklentisini oluşturur (zararsız, yalnızca kopyada)
    log "pg_amcheck (heap + btree yapısal bütünlük, 8 paralel) — hedef diskinde, prod'a dokunmaz"
    hedef "docker exec $DOGRULA_KONTEYNER pg_amcheck -U $DB_KULLANICI -d $DB_ADI --install-missing -j 8" \
      || die "pg_amcheck BOZULMA buldu — bu kopyayı kullanmayın"
    log "✓ pg_amcheck temiz"
  fi

  hedef "docker rm -f $DOGRULA_KONTEYNER >/dev/null"
  hedef "printf '%s\n' 'tarih: $(TZ=Europe/Istanbul date '+%F %T') TR' 'kaynak: 91.241.49.109' \
           'imaj: $PG_IMAJ' 'volume: $HEDEF_VOLUME' 'yedek: $HEDEF_DIZIN/base.tar' > '$HEDEF_DIZIN/TAMAM'"
}

# ── Asıl iş ─────────────────────────────────────────────────────────────────────
calistir() {
  mkdir -p "$CALISMA_DIZINI/log"
  LOG="$CALISMA_DIZINI/log/tasima-$(date -u +%Y%m%d-%H%M%S).log"
  exec > >(tee -a "$LOG") 2>&1
  ln -sfn "$LOG" "$CALISMA_DIZINI/son.log"

  exec 9>"$CALISMA_DIZINI/kilit"
  flock -n 9 || die "başka bir taşıma zaten çalışıyor"

  trap temizlik EXIT
  trap 'exit 130' INT TERM

  log "=== DB TAŞIMA BAŞLIYOR === hedef=$HEDEF volume=$HEDEF_VOLUME"
  on_kontrol || die "ön kontrol başarısız — HİÇBİR ŞEY YAPILMADI"
  trap 'log "✗ satır $LINENO: başarısız komut: $BASH_COMMAND"' ERR

  local boyut rc tar_boyut
  boyut=$(db_boyut_gb)

  if [ "$(hedef_volume_durumu)" = dolu ]; then
    log "! ZORLA=1 — hedef volume siliniyor"
    hedef "docker rm -f $DOGRULA_KONTEYNER >/dev/null 2>&1; docker volume rm '$HEDEF_VOLUME' >/dev/null" \
      || die "hedef volume silinemedi (kullanan konteyner var mı?)"
  fi
  hedef "mkdir -p '$HEDEF_DIZIN' && rm -f '$HEDEF_DIZIN/base.tar.partial' '$HEDEF_DIZIN/TAMAM'"
  slot_sil

  if [ "$WORKERLARI_DURAKLAT" = 1 ]; then
    log "beat + worker'lar duraklatılıyor (en geç $(TZ=Europe/Istanbul date -d "@$(sonraki_tr_epoch "$WORKER_DONUS_TR")" '+%F %H:%M') TR'de geri açılır)"
    workerlari_duraklat
  fi

  pgq "SELECT pg_create_physical_replication_slot('$SLOT', true)" >/dev/null || die "replikasyon slotu açılamadı"
  SLOT_ACIK=1
  log "slot '$SLOT' açıldı (yedek süresince WAL tutuluyor)"

  bekci &
  BEKCI_PID=$!

  local bb_ek=()
  [ -n "$MAX_RATE" ] && bb_ek=(-r "$MAX_RATE")
  log "pg_basebackup başladı (~${boyut} GB, prod → $HEDEF:$HEDEF_DIZIN/base.tar)"
  YEDEK_SURUYOR=1
  # ⚠️⚠️ ERR trap'i boru süresince KAPALI: boru başarısız olursa trap, PIPESTATUS
  #  okunmadan ÖNCE çalışır ve kendi komutlarıyla (log/printf) onu ezer → başarısız
  #  bir yedek "kod 0" okunup BAŞARILI sayılırdı.
  trap - ERR
  set +e
  # -c spread: başlangıç checkpoint'i yayılır — hızlı checkpoint kısıtlı diskte yazma fırtınası olurdu
  docker exec "$DB_KONTEYNER" pg_basebackup -U "$DB_KULLANICI" -D - -Ft -X fetch -c spread \
      ${bb_ek[@]+"${bb_ek[@]}"} 2>>"$LOG" \
    | ssh "${SSH_AKIS[@]}" "$HEDEF" "cat > '$HEDEF_DIZIN/base.tar.partial'"
  rc=("${PIPESTATUS[@]}")
  set -e
  trap 'log "✗ satır $LINENO: başarısız komut: $BASH_COMMAND"' ERR
  YEDEK_SURUYOR=0
  kill "$BEKCI_PID" 2>/dev/null || true
  BEKCI_PID=""
  [ "${rc[0]}" = 0 ] || die "pg_basebackup başarısız (kod ${rc[0]}) — ayrıntı yukarıdaki log satırlarında"
  [ "${rc[1]}" = 0 ] || die "hedefe aktarım (ssh) başarısız (kod ${rc[1]})"

  hedef "mv '$HEDEF_DIZIN/base.tar.partial' '$HEDEF_DIZIN/base.tar'"
  tar_boyut=$(hedef "du -h '$HEDEF_DIZIN/base.tar' | cut -f1")
  log "✓ yedek aktarıldı: $HEDEF:$HEDEF_DIZIN/base.tar ($tar_boyut) — $(sure_bicim $(( $(date +%s) - BASLANGIC )))"

  # WAL artık tar'ın içinde → slot gerekmez (SLOTU_KORU=1 ise replika tabanlı kesme için tutulur)
  [ "$SLOTU_KORU" = 1 ] || slot_sil
  # prod'daki iş bitti; bundan sonrası yalnızca hedef diskini kullanır
  workerlari_ac
  referans_al
  geri_yukle_ve_dogrula

  log "=== TAŞIMA TAMAM === volume '$HEDEF_VOLUME' hazır · sunucu dışı yedek: $HEDEF:$HEDEF_DIZIN/base.tar"
}

# ── Yalnızca doğrulama (kopyayı yeniden almadan) ────────────────────────────────
# 5 saatlik aktarım başarılı olup doğrulama geçici bir sebeple düştüyse (hedef
# yeniden başladı, SSH koptu…) kopyayı baştan almamak için. Hedefteki base.tar'ı
# ve yedek anında kaydedilen prod referansını kullanır.
dogrula() {
  mkdir -p "$CALISMA_DIZINI/log"
  LOG="$CALISMA_DIZINI/log/dogrula-$(date -u +%Y%m%d-%H%M%S).log"
  exec > >(tee -a "$LOG") 2>&1
  ln -sfn "$LOG" "$CALISMA_DIZINI/son.log"
  exec 9>"$CALISMA_DIZINI/kilit"
  flock -n 9 || die "başka bir taşıma zaten çalışıyor"
  trap temizlik EXIT
  trap 'exit 130' INT TERM

  [ -s "$CALISMA_DIZINI/referans.sql" ] && [ -s "$CALISMA_DIZINI/referans-prod.txt" ] \
    || die "kayıtlı prod referansı yok — önce 'calistir' tamamlanmalı"
  hedef "test -s '$HEDEF_DIZIN/base.tar'" || die "hedefte $HEDEF_DIZIN/base.tar yok"
  PG_IMAJ=$(pg_imaj)
  REFERANS_SQL=$(cat "$CALISMA_DIZINI/referans.sql")
  case "$(hedef_volume_durumu)" in
    dolu) [ "$ZORLA" = 1 ] || die "hedef volume dolu — yeniden açmak için ZORLA=1"
          hedef "docker rm -f $DOGRULA_KONTEYNER >/dev/null 2>&1; docker volume rm '$HEDEF_VOLUME' >/dev/null" \
            || die "hedef volume silinemedi" ;;
  esac
  log "=== YALNIZCA DOĞRULAMA === (kaynak: $HEDEF:$HEDEF_DIZIN/base.tar)"
  trap 'log "✗ satır $LINENO: başarısız komut: $BASH_COMMAND"' ERR
  geri_yukle_ve_dogrula
  log "=== DOĞRULAMA TAMAM ==="
}

# ── Zamanlama ───────────────────────────────────────────────────────────────────
zamanla() {
  local saat=${1:-} epoch takvim v
  local env_args=()
  [[ "$saat" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "kullanım: $0 zamanla HH:MM (Türkiye saati)"
  mkdir -p "$CALISMA_DIZINI"
  if systemctl is-active --quiet "$TIMER_ADI.timer" || systemctl is-active --quiet "$TIMER_ADI.service"; then
    die "zaten kurulu/çalışan bir taşıma var ($0 durum · $0 iptal)"
  fi
  epoch=$(sonraki_tr_epoch "$saat")
  takvim=$(date -u -d "@$epoch" '+%Y-%m-%d %H:%M:%S UTC')
  # ⚠️ Donmuş kopya: gece çalışırken `git pull`/`install.sh` betiği değiştirmesin
  cp "$(readlink -f "$0")" "$CALISMA_DIZINI/calisan.sh"
  chmod 700 "$CALISMA_DIZINI/calisan.sh"
  for v in DB_KONTEYNER DB_KULLANICI DB_ADI HEDEF HEDEF_DIZIN HEDEF_VOLUME DOGRULA_KONTEYNER SLOT MAX_RATE \
           WORKERLARI_DURAKLAT WORKER_DONUS_TR SON_SAAT_TR AMCHECK ZORLA SLOTU_KORU MIN_PROD_BOS_GB \
           KRITIK_PROD_BOS_GB CALISMA_DIZINI; do
    env_args+=(--setenv="$v=${!v}")
  done
  # TimeoutStopSec: `iptal` SIGTERM gönderince trap'in slotu silip worker'ları açmasına zaman kalsın
  systemd-run --unit="$TIMER_ADI" --on-calendar="$takvim" --timer-property=AccuracySec=1s \
    --property=TimeoutStopSec=300 --setenv=HOME=/root "${env_args[@]}" \
    /bin/bash "$CALISMA_DIZINI/calisan.sh" calistir
  log "zamanlandı: $(TZ=Europe/Istanbul date -d "@$epoch" '+%F %H:%M') TR ($takvim)"
  log "izlemek için: $0 durum   ·   canlı log: tail -f $CALISMA_DIZINI/son.log"
}

durum() {
  echo "=== zamanlayıcı ==="
  systemctl list-timers "$TIMER_ADI.timer" --all --no-pager 2>/dev/null | head -3 || true
  echo "servis: $(systemctl is-active "$TIMER_ADI.service" 2>/dev/null || true)"
  echo "=== son durum ==="
  cat "$CALISMA_DIZINI/son-durum.txt" 2>/dev/null || echo "(henüz çalışmadı)"
  echo "=== son log (25 satır) ==="
  tail -25 "$CALISMA_DIZINI/son.log" 2>/dev/null || echo "(log yok)"
  echo "=== replikasyon slotları ==="
  pgq "SELECT slot_name || ' aktif=' || active || ' tutulan_wal=' ||
              coalesce(pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)), '-')
         FROM pg_replication_slots" 2>/dev/null || true
  echo "=== worker / beat ==="
  docker ps -a --format '{{.Names}}  {{.Status}}' | grep -E '^ihaletakip-api-.*(worker|beat)' || true
}

iptal() {
  systemctl stop "$TIMER_ADI.timer" 2>/dev/null || true
  if systemctl is-active --quiet "$TIMER_ADI.service"; then
    log "çalışan taşıma durduruluyor (trap slotu silip worker'ları açacak)"
    systemctl stop "$TIMER_ADI.service" || true
  fi
  systemctl reset-failed "$TIMER_ADI.timer" "$TIMER_ADI.service" 2>/dev/null || true
  log "zamanlayıcı kaldırıldı"
}

kullanim() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

# ── Giriş ───────────────────────────────────────────────────────────────────────
[ "$(id -u)" = 0 ] || die "root olarak çalıştırın"
case "${1:-}" in
  on-kontrol) mkdir -p "$CALISMA_DIZINI"; on_kontrol ;;
  calistir)   calistir ;;
  dogrula)    dogrula ;;
  zamanla)    shift; zamanla "$@" ;;
  durum)      durum ;;
  iptal)      iptal ;;
  *)          kullanim; exit 2 ;;
esac
