#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# gecis_delta.sh — uygulama taşımasında "kopya sonrası" farkı eski sunucudan
# yeni sunucuya taşır. Bu betik BU MAC'ten koşar (iki sunucuya da SSH'ı var).
#
# NEDEN VAR: veritabanı kopyası (pg_basebackup) bir ANDA donar. O andan kesmeye
# kadar eski sunucuda biriken küçük durumu taşımazsak iki sessiz arıza doğar:
#
#   1) ⚠️⚠️ KULLANICILARA MÜKERRER BİLDİRİM. `django_celery_beat_periodictask.
#      last_run_at` kopyada eski kalır; yeni sunucuda beat başlayınca GÜNLÜK
#      görevler (07:00 asistan digest, 08:00 OKAS önerisi, 09:00 alarm) "bugün
#      hiç koşmamış" görünür ve **hemen** tetiklenir → kullanıcı aynı push'u
#      ikinci kez alır. Ölçüldü (2026-09-16): prod'da bu üç görev o gün çoktan
#      koşmuştu, kopyadaki damga ise 15 Eylül'dü.
#   2) Uygulama içi bildirim satırlarının kaybı — push telefona gitmiştir ama
#      satır kopyada yoktur, kullanıcının bildirim listesinde delik açar.
#
# İkinci katman olarak Redis'teki mükerrerlik-engeli işaretleri de taşınır
# (`alarm:`/`okas:`/`teaser:`/`digest:`/`filter:`/`authority:` gün kilitleri).
# `last_run_at` birincil korumadır; Redis kilitleri kemer-askı.
#
# ⚠️ EKAP verisi (ihale/sözleşme) BİLİNÇLİ OLARAK TAŞINMAZ: yeniden çekilebilir,
# `sync_recent` + mobil keşif boşluğu kendiliğinden kapatır. Taşınan yalnızca
# yeniden üretilemeyen durumdur.
#
# ⚠️ ÖNCE eski sunucuda beat + worker'lar DURDURULMUŞ olmalı, yoksa damgalar
# okunduktan sonra değişmeye devam eder ve fark kapanmaz.
#
# Kullanım:
#   scripts/gecis_delta.sh kontrol     # hiçbir şey yazmaz, farkı gösterir
#   scripts/gecis_delta.sh uygula      # farkı yeni sunucuya yazar
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ESKI="${ESKI:-root@91.241.49.109}"
YENI="${YENI:-tinyfect}"
ESKI_DB="${ESKI_DB:-ihaletakip-api-db-1}"
YENI_DB="${YENI_DB:-ihaletakip-api-db-1}"
ESKI_REDIS="${ESKI_REDIS:-ihaletakip-api-redis-1}"
YENI_REDIS="${YENI_REDIS:-ihaletakip-api-redis-1}"
KULLANICI="${KULLANICI:-ihale}"
VERITABANI="${VERITABANI:-ihaletakip}"
SSH_OPT=(-o ConnectTimeout=30 -o ServerAliveInterval=15 -o BatchMode=yes)
IS_DIZINI="${IS_DIZINI:-$(mktemp -d)}"

kayit() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
oldu()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
uyari() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
hata()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

# psql'i uzaktan koşturan yardımcılar. ⚠️ `ssh -n`: stdin'i tüketen bir komut
# (docker exec -i) betiğin geri kalanını yutabiliyor (bkz. CLAUDE.md db_tasima).
eski_sql()  { ssh -n "${SSH_OPT[@]}" "$ESKI" "docker exec $ESKI_DB psql -U $KULLANICI -d $VERITABANI -tA -F'|' -c \"$1\""; }
yeni_sql()  { ssh -n "${SSH_OPT[@]}" "$YENI" "docker exec $YENI_DB psql -U $KULLANICI -d $VERITABANI -tA -F'|' -c \"$1\""; }
# Dosyadan SQL çalıştır (stdin kullanır → -n YOK).
yeni_dosya(){ ssh "${SSH_OPT[@]}" "$YENI" "docker exec -i $YENI_DB psql -U $KULLANICI -d $VERITABANI -v ON_ERROR_STOP=1 -f -"; }

# ── 1) Beat damgaları ────────────────────────────────────────────────────────
# Tüm PeriodicTask satırları için last_run_at/total_run_count'u ADA GÖRE günceller.
# ⚠️ Tabloyu KOPYALAMAYIZ: takvim tanımları (crontab FK'ları, enabled) kopyada
# zaten doğru ve kod tarafından yönetiliyor; ezmek istemediğimiz tek şey onlar.
beat_damgalari() {
  local f="$IS_DIZINI/beat.sql"
  eski_sql "SELECT format('UPDATE django_celery_beat_periodictask SET last_run_at=%L, total_run_count=%s WHERE name=%L;', last_run_at, total_run_count, name) FROM django_celery_beat_periodictask WHERE last_run_at IS NOT NULL" > "$f" 2>/dev/null
  local n; n=$(grep -c '^UPDATE' "$f" 2>/dev/null || echo 0)
  if [ "$n" -eq 0 ]; then uyari "beat damgası bulunamadı (atlandı)"; return 0; fi
  if [ "$1" = "uygula" ]; then
    # ⚠️ `PeriodicTasks.update_changed()` sinyali BİLEREK tetiklenmez: burada
    # takvim DEĞİŞMİYOR, yalnızca "en son ne zaman koştu" damgası yazılıyor.
    # (Takvimi açıp kapatırken ham SQL yetmez — bkz. CLAUDE.md config/celery.py.)
    printf 'BEGIN;\n%s\nCOMMIT;\n' "$(cat "$f")" | yeni_dosya >/dev/null && oldu "$n beat damgası yazıldı" || hata "beat damgaları yazılamadı"
  else
    oldu "$n beat damgası aktarılacak"
    grep -E "assistant|okas|alarm|filter|authority|contractor|teaser" "$f" | head -8 | sed 's/^/      /'
  fi
}

# ── 2) Bildirim satırları ────────────────────────────────────────────────────
# Kopyadaki en büyük id'den sonrasını COPY ile taşır (pg_dump satır filtresi
# desteklemiyor). Kolon listesi iki tarafta da aynı olmalı.
bildirimler() {
  local enb kolonlar
  enb=$(yeni_sql "SELECT coalesce(max(id),0) FROM tenders_notification" | tr -d ' \r')
  [ -z "$enb" ] && { hata "yeni sunucudan bildirim id'si okunamadı"; return 1; }
  kolonlar=$(yeni_sql "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_schema='public' AND table_name='tenders_notification'" | tr -d ' \r')
  local n; n=$(eski_sql "SELECT count(*) FROM tenders_notification WHERE id > $enb" | tr -d ' \r')
  if [ "${n:-0}" -eq 0 ]; then oldu "eksik bildirim yok"; return 0; fi
  if [ "$1" != "uygula" ]; then oldu "$n bildirim satırı aktarılacak (id > $enb)"; return 0; fi
  ssh -n "${SSH_OPT[@]}" "$ESKI" "docker exec $ESKI_DB psql -U $KULLANICI -d $VERITABANI -c \"\\copy (SELECT $kolonlar FROM tenders_notification WHERE id > $enb ORDER BY id) TO STDOUT\"" > "$IS_DIZINI/bildirim.tsv" 2>/dev/null
  [ -s "$IS_DIZINI/bildirim.tsv" ] || { uyari "bildirim verisi boş geldi (atlandı)"; return 0; }
  ssh "${SSH_OPT[@]}" "$YENI" "docker exec -i $YENI_DB psql -U $KULLANICI -d $VERITABANI -v ON_ERROR_STOP=1 -c \"\\copy tenders_notification ($kolonlar) FROM STDIN\"" < "$IS_DIZINI/bildirim.tsv" >/dev/null \
    && oldu "$n bildirim satırı yazıldı" || hata "bildirim satırları yazılamadı"
  # ⚠️ Diziyi ilerlet: elle id yazdık, sequence geride kalırsa sonraki INSERT çakışır.
  yeni_sql "SELECT setval(pg_get_serial_sequence('tenders_notification','id'), (SELECT max(id) FROM tenders_notification))" >/dev/null 2>&1
}

# ── 3) Redis mükerrerlik kilitleri ───────────────────────────────────────────
# DUMP/RESTORE ile birebir (tip + TTL korunur). base64, ikili veriyi SSH üzerinden
# güvenle taşımak için — django-redis değerleri pickle'lı bayttır, düz metin değil.
redis_kilitleri() {
  local py_oku py_yaz
  py_oku='
import base64, sys
from redis import Redis
r = Redis(host="localhost", port=6379, db=0)
n = 0
for kalip in ("*alarm:*","*okas:*","*teaser:*","*digest:*","*filter:*","*authority:*"):
    for k in r.scan_iter(match=kalip, count=500):
        try:
            v = r.dump(k)
            if v is None:
                continue
            t = r.pttl(k)
            if t is None or t < 0:
                t = 0
            sys.stdout.write("%s\t%s\t%d\n" % (
                base64.b64encode(k).decode(), base64.b64encode(v).decode(), t))
            n += 1
        except Exception:
            pass
sys.stderr.write("okunan=%d\n" % n)
'
  py_yaz='
import base64, sys
from redis import Redis
r = Redis(host="localhost", port=6379, db=0)
ok = at = 0
for satir in sys.stdin:
    p = satir.rstrip("\n").split("\t")
    if len(p) != 3:
        continue
    k, v, t = base64.b64decode(p[0]), base64.b64decode(p[1]), int(p[2])
    try:
        # replace=True: aynı anahtar varsa üzerine yaz (idempotent tekrar koşu).
        r.restore(k, t, v, replace=True)
        ok += 1
    except Exception:
        at += 1
print("yazilan=%d atlanan=%d" % (ok, at))
'
  # Redis kütüphanesi web konteynerinde var (Django cache backend'i), redis
  # konteynerinde yok → okuma/yazma web üzerinden, ağ içi localhost yerine servis adı.
  local f="$IS_DIZINI/redis.tsv"
  ssh -n "${SSH_OPT[@]}" "$ESKI" "docker exec -e PYTHONWARNINGS=ignore ihaletakip-api-web-1 python -c '${py_oku//localhost/redis}'" > "$f" 2>/dev/null
  local n; n=$(wc -l < "$f" | tr -d ' ')
  if [ "${n:-0}" -eq 0 ]; then uyari "Redis kilidi bulunamadı (atlandı — beat damgaları yine de korur)"; return 0; fi
  if [ "$1" != "uygula" ]; then oldu "$n Redis kilidi aktarılacak"; return 0; fi
  ssh "${SSH_OPT[@]}" "$YENI" "docker exec -i -e PYTHONWARNINGS=ignore ihaletakip-api-web-1 python -c '${py_yaz//localhost/redis}'" < "$f" | sed 's/^/      /'
  oldu "$n Redis kilidi aktarıldı"
}

# ── 4) Doğrulama ─────────────────────────────────────────────────────────────
dogrula() {
  kayit "doğrulama"
  printf '      %-34s %-22s %s\n' "" "ESKİ" "YENİ"
  for s in \
    "django_celery_beat_periodictask|SELECT to_char(max(last_run_at),'YYYY-MM-DD HH24:MI')" \
    "tenders_notification|SELECT max(id)::text" \
    "accounts_user|SELECT max(id)::text" \
  ; do
    local t="${s%%|*}" q="${s#*|}"
    printf '      %-34s %-22s %s\n' "$t" \
      "$(eski_sql "$q FROM $t" | tr -d ' \r')" "$(yeni_sql "$q FROM $t" | tr -d ' \r')"
  done
}

MOD="${1:-kontrol}"
case "$MOD" in
  kontrol|uygula) ;;
  *) echo "kullanım: $0 [kontrol|uygula]" >&2; exit 2 ;;
esac

kayit "mod: $MOD   (eski: $ESKI → yeni: $YENI)   iş dizini: $IS_DIZINI"
if [ "$MOD" = "uygula" ]; then
  calisan=$(ssh -n "${SSH_OPT[@]}" "$ESKI" "docker ps --format '{{.Names}}' | grep -cE 'beat|worker'" 2>/dev/null | tr -d ' \r')
  if [ "${calisan:-0}" -gt 0 ]; then
    hata "eski sunucuda $calisan beat/worker HÂLÂ ÇALIŞIYOR — damgalar donmadan aktarım anlamsız."
    echo "     Önce: ssh $ESKI 'cd /root/ihaletakip-api && docker stop \$(docker ps -q -f name=beat -f name=worker)'" >&2
    exit 1
  fi
  oldu "eski sunucuda beat/worker durmuş"
fi
kayit "1/3 beat damgaları";      beat_damgalari "$MOD"
kayit "2/3 bildirim satırları";  bildirimler    "$MOD"
kayit "3/3 Redis kilitleri";     redis_kilitleri "$MOD"
dogrula
kayit "bitti"
