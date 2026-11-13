#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# yedek_ftp.sh — veritabanının gecelik yedeğini alıp Contabo FTP alanına atar.
#
# tinyfect'in kendi `backup_postgres.sh`'i örnek alındı ama üç yerde bilinçli
# olarak ayrıldı (gerekçeleri aşağıda): FTPS, uzak temizlik, doğrulama.
#
#   scripts/yedek_ftp.sh test      # bağlantı + yetki denemesi, yedek ALMAZ
#   scripts/yedek_ftp.sh al        # yedek al + yükle + temizle (cron bunu çağırır)
#   scripts/yedek_ftp.sh saatlik   # SAATLİK kullanıcı verisi yedeği (EKAP tabloları hariç; cron :35)
#                                  #   KURU=1 → dump + doğrulama, yükleme/silme YOK
#   scripts/yedek_ftp.sh durum     # son yedeğin durumu ve tazeliği
#   scripts/yedek_ftp.sh liste     # FTP'deki kendi yedeklerimiz
#   scripts/yedek_ftp.sh temizle   # yalnızca saklama kuralını uygula
#   scripts/yedek_ftp.sh ayar        # yalnızca şifreli sır yedeğini al
#   scripts/yedek_ftp.sh ayar-coz    # şifreli sır yedeğini çöz (kurtarmada)
#
# AYARLAR: /etc/ihaletakip-yedek.env (chmod 600 — FTP şifresi içerir).
# ⚠️ Şifre BETİĞE GÖMÜLMEZ: tinyfect'in betiği `--user kullanici:sifre` diye
# gömüyor ve dosya 755 (herkes okuyabilir). Bu betik repo'da durduğu için
# gömmek şifreyi doğrudan git geçmişine yazmak olurdu.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

AYAR_DOSYASI="${AYAR_DOSYASI:-/etc/ihaletakip-yedek.env}"
[ -r "$AYAR_DOSYASI" ] && . "$AYAR_DOSYASI"

DB_KONTEYNER="${DB_KONTEYNER:-ihaletakip-api-db-1}"
DB_KULLANICI="${DB_KULLANICI:-ihale}"
DB_ADI="${DB_ADI:-ihaletakip}"
YEREL_DIZIN="${YEREL_DIZIN:-/var/backups/ihaletakip}"
LOG="${LOG:-/var/log/ihaletakip-yedek.log}"
KILIT="${KILIT:-/var/lock/ihaletakip-yedek.lock}"
ONEK="${ONEK:-ihaletakip}"

# ⚠️ UZAK DİZİN AYRI: tinyfect'in yedekleri FTP kökünde (`spark_*`). Aynı yere
# yazmak, temizlik mantığının yanlış dosyayı silme riskini doğurur. Ayrı dizin
# + aşağıdaki katı ad kalıbı **iki katmanlı** koruma sağlar.
UZAK_DIZIN="${UZAK_DIZIN:-ihaletakip}"

# Saklama. ⚠️ tinyfect'in betiğinde UZAK TEMİZLİK HİÇ YOK → FTP'de Mart'tan beri
# 171 dosya / 97 GB birikmiş (çoğu artık kullanılmayan MSSQL .bak'ları). 250 GB'lık
# alanın dolması yedeklemenin **sessizce durması** demek olurdu.
GUNLUK_SAKLA="${GUNLUK_SAKLA:-14}"      # son N günlük yedek
AYLIK_SAKLA="${AYLIK_SAKLA:-12}"        # ayın 1'ine ait yedekler, N ay
YEREL_SAKLA_GUN="${YEREL_SAKLA_GUN:-3}" # yerelde kaç gün tutulsun

SIKISTIRMA="${SIKISTIRMA:-6}"           # pg_dump -Z
YUKLEME_DENEME="${YUKLEME_DENEME:-3}"
FTP_ZAMAN_ASIMI="${FTP_ZAMAN_ASIMI:-7200}"

# ⚠️⚠️ ALAN KORUMASI. FTP'nin kotası sabittir (Contabo yedek alanı: 250 GB) ve
# **sorgulanabilir bir kota API'si YOK** — ProFTPD `SITE QUOTA`/`AVBL` desteklemiyor
# (denendi). Kota dolduğunda yükleme yarıda kesilir, curl bazen başarı döner ve
# yedekleme **sessizce bozulur**. Bu yüzden kullanım her turda dosya boyutları
# toplanarak hesaplanır ve yeni dump'a yer yoksa önce temizlik yapılır.
FTP_KOTA_GB="${FTP_KOTA_GB:-250}"
FTP_UYARI_ORAN="${FTP_UYARI_ORAN:-90}"  # yüzde; aşılırsa log'a uyarı düşer

# ── Ayar/sır yedeği ──────────────────────────────────────────────────────────
# ⚠️⚠️ Yalnızca veritabanıyla kurtarma YAPILAMAZ: `.env.prod` olmadan konteynerler
# açılmaz, `credentials/` olmadan push ve TTS çalışmaz, origin sertifikası olmadan
# Cloudflare 522 verir. Bu yüzden onlar da yedeklenir — ama **şifreli**.
# ⚠️⚠️ ŞİFRESİZ ASLA YÜKLENMEZ: FTP alanı paylaşımlı ve dosyalar orada açık metin
# durur. `AYAR_SIFRE` boşsa yedek ATLANIR (sessizce açık göndermek yerine).
REPO_DIZINI="${REPO_DIZINI:-$(cd "$(dirname "$0")/.." && pwd)}"
AYAR_YEDEK="${AYAR_YEDEK:-1}"
AYAR_SAKLA="${AYAR_SAKLA:-30}"          # küçük dosyalar (~20 KB) → comert
# Yedeklenecek yollar (repo köküne göre).
AYAR_YOLLARI="${AYAR_YOLLARI:-.env.prod credentials docker/nginx/certs}"

# ── Saatlik kullanıcı verisi yedeği (2026-11-13) ─────────────────────────────
# ⚠️ NEDEN: 2026-11-11 çöküşünde gecelik 04:00 yedeği yüzünden ~15 saatlik KULLANICI verisi
# (kayıt, filtre, sohbet, abonelik, destek) yalnız ölü sunucunun diskinde kaldı; geri dönüşte
# elle 3 yollu birleştirme gerekti. Tam dump saatlik ALINAMAZ (5,7 GB / 15 dk; 72 kopya ≈ 410 GB >
# 250 GB kota). Ama yeniden üretilemeyen veri küçük: `ekap_*` dışındaki tablolar ~16 MB.
# → Her saat: TÜM şema + `ekap_*` HARİÇ tüm tabloların verisi (beat damgaları ve ayarlar dahil).
# Kurtarma: gecelik tam dump'ı aç → üstüne son saatlik dump'ın kullanıcı tablolarını yükle
# (backend CLAUDE.md "Gecelik veritabanı yedeği" → "Saatlik katman").
# ⚠️ EKAP verisi EKAP'tan yeniden toplanır; ama kullanıcı tablolarındaki 3 FK (öneri→ihale,
# firma profili/takip→firma) ID ile değil DOĞAL anahtarla (ekap_id / kanonik_anahtar) eşlenmeli.
SAATLIK_DIZIN="${SAATLIK_DIZIN:-saatlik}"            # UZAK_DIZIN altında
SAATLIK_SAKLA="${SAATLIK_SAKLA:-72}"                 # FTP'de son N saat (3 gün)
SAATLIK_YEREL_SAKLA="${SAATLIK_YEREL_SAKLA:-48}"     # yerelde son N dosya
SAATLIK_HARIC="${SAATLIK_HARIC:-ekap_*}"             # VERİSİ alınmayan tablo kalıpları (şema yine alınır)
SAATLIK_MIN_BAYT="${SAATLIK_MIN_BAYT:-102400}"       # 100 KB altı şüpheli (2026-11'de ~3 MB)
KILIT_SAATLIK="${KILIT_SAATLIK:-/var/lock/ihaletakip-yedek-saatlik.lock}"
LOG_SAATLIK="${LOG_SAATLIK:-/var/log/ihaletakip-yedek-saatlik.log}"
KURU="${KURU:-0}"

# ⚠️ ŞİFRELİ FTP (FTPS) VARSAYILAN. Ölçüldü (2026-09-16): sunucu ProFTPD ve
# `AUTH SSL` çalışıyor ("234 AUTH SSL successful"). tinyfect'in betiği düz
# `ftp://` kullanıyor → şifre VE veritabanının tamamı ağda **açık metin** akıyor.
# Yedek 146 kullanıcının kişisel verisini taşıdığı için bu KVKK açısından da
# önemli. `FTPS=0` yalnızca sunucu TLS'i bozarsa geçici kaçış yoludur.
FTPS="${FTPS:-1}"

kayit() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }
oldu()  { kayit "  ✓ $*"; }
uyari() { kayit "  ⚠ $*"; }
hata()  { kayit "  ✗ $*"; }

ftp_tabani() {
  local sema="ftp"
  printf '%s://%s/%s/' "$sema" "${FTP_HOST:?FTP_HOST tanımsız ($AYAR_DOSYASI)}" "${UZAK_DIZIN#/}"
}

# curl ortak argümanları. --ssl-reqd: TLS kurulamazsa BAŞARISIZ ol (sessizce
# düz metne düşmesin — "şifreli sanıp açık göndermek" en kötü hâl).
curl_args() {
  local -n _d=$1
  _d=(--silent --show-error --user "${FTP_USER:?FTP_USER tanımsız}:${FTP_PASS:?FTP_PASS tanımsız}"
      --connect-timeout 30 --max-time "$FTP_ZAMAN_ASIMI" --ftp-create-dirs)
  [ "$FTPS" = "1" ] && _d+=(--ssl-reqd)
}

# FTP'de kullanılan toplam bayt (kök + alt dizinlerimiz). ⚠️ `ls -R` yok →
# kök ve kendi dizinimiz ayrı ayrı toplanır; başka projenin alt dizinleri varsa
# eksik sayar, bu yüzden sonuç **alt sınırdır** (temkinli tarafta hata yapar).
ftp_kullanim_bayt() {
  local A; curl_args A
  local kok bizim
  local saatlik
  kok=$(curl "${A[@]}" "ftp://${FTP_HOST}/" 2>/dev/null | tr -d '\r' | awk '$5 ~ /^[0-9]+$/ {s+=$5} END {print s+0}')
  bizim=$(curl "${A[@]}" "$(ftp_tabani)" 2>/dev/null | tr -d '\r' | awk '$5 ~ /^[0-9]+$/ {s+=$5} END {print s+0}')
  saatlik=$(curl "${A[@]}" "$(ftp_tabani)${SAATLIK_DIZIN}/" 2>/dev/null | tr -d '\r' | awk '$5 ~ /^[0-9]+$/ {s+=$5} END {print s+0}')
  echo $(( ${kok:-0} + ${bizim:-0} + ${saatlik:-0} ))
}

# Yeni dump için yer var mı? Yoksa ÖNCE temizlik dener, sonra tekrar bakar.
# ⚠️ Temizlik normalde yüklemeden SONRA yapılır (yeni yedek sağlamken eskiyi
# silmek doğru sıra). Burada sıra bilinçli olarak tersine çevrilir: yer yoksa
# yükleme zaten başarısız olacağı için eskiyi tutmanın bir faydası kalmaz.
yer_var_mi() {
  local gereken="$1" kullanim kota_bayt esik
  kota_bayt=$(( FTP_KOTA_GB * 1073741824 ))
  kullanim=$(ftp_kullanim_bayt)
  esik=$(( kota_bayt * FTP_UYARI_ORAN / 100 ))
  kayit "  FTP kullanımı: $(numfmt --to=iec "$kullanim" 2>/dev/null || echo "$kullanim")/${FTP_KOTA_GB}G"
  [ "$kullanim" -gt "$esik" ] && uyari "FTP kullanımı %${FTP_UYARI_ORAN} eşiğini aştı — saklama süresini kısaltmayı düşünün"
  if [ $(( kullanim + gereken )) -lt "$kota_bayt" ]; then return 0; fi
  uyari "yeni yedek için yer yetersiz — önce temizlik deneniyor"
  mod_temizle
  kullanim=$(ftp_kullanim_bayt)
  if [ $(( kullanim + gereken )) -lt "$kota_bayt" ]; then
    oldu "temizlikten sonra yer açıldı"; return 0
  fi
  hata "temizlikten sonra da yer yok (kullanım $(numfmt --to=iec "$kullanim" 2>/dev/null), gereken $(numfmt --to=iec "$gereken" 2>/dev/null))"
  return 1
}

# Durumu hem dosyaya hem `core_appsetting`'e yazar → admin panelinden görünür
# (⚠️ sessiz yedek arızası bu işin klasik felaketidir; SSH gerektirmeyen bir
# görünürlük şart). DB yazımı başarısız olursa iş DÜŞMEZ, yalnızca loglanır.
durum_yaz() {
  local sonuc="$1" mesaj="$2"
  local anahtar="${3:-yedek_durumu}" dosya="${4:-son-durum.txt}"
  local aciklama="${5:-Gecelik FTP yedeğinin son durumu (yedek_ftp.sh yazar)}"
  mkdir -p "$YEREL_DIZIN"
  printf '%s|%s|%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$sonuc" "$mesaj" > "$YEREL_DIZIN/$dosya"
  local deger; deger=$(printf '%s — %s (%s)' "$sonuc" "$mesaj" "$(date '+%Y-%m-%d %H:%M %Z')")
  docker exec "$DB_KONTEYNER" psql -U "$DB_KULLANICI" -d "$DB_ADI" -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO core_appsetting (key, value, description, updated_at)
     VALUES ('$anahtar', \$\$$deger\$\$, '$aciklama', now())
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()" >/dev/null 2>&1 \
    || uyari "durum core_appsetting'e yazılamadı (yedek yine de geçerli)"
}

saatlik_durum_yaz() {
  durum_yaz "$1" "$2" yedek_saatlik_durumu son-saatlik.txt \
    "Saatlik kullanıcı verisi FTP yedeğinin son durumu (yedek_ftp.sh saatlik yazar)"
}

# ── Ayar/sır yedeği (GPG AES256, simetrik) ───────────────────────────────────
# Simetrik şifre (anahtar çifti değil) bilinçli: felaket kurtarmada elinizde
# yalnızca bir parola olması, bir özel anahtar dosyası aramaktan kolaydır.
# ⚠️ PAROLA SUNUCUDA VE PAROLA YÖNETİCİSİNDE OLMALI. Yalnızca sunucuda durursa,
# sunucu kaybolduğunda yedek de okunamaz — yani hiç yedek almamışla aynı olur.
ayar_yedegi() {
  [ "$AYAR_YEDEK" = "1" ] || { oldu "ayar yedeği kapalı (AYAR_YEDEK=0)"; return 0; }
  if [ -z "${AYAR_SIFRE:-}" ]; then
    uyari "AYAR_SIFRE tanımsız → ayar yedeği ATLANDI (şifresiz yüklenmez)"
    return 0
  fi
  local ad="${ONEK}-ayar_$(date +%Y%m%d_%H%M).tar.gz.gpg"
  local gecici sifre_dosyasi
  gecici=$(mktemp "$YEREL_DIZIN/.ayar.XXXXXX") || return 1
  sifre_dosyasi=$(mktemp) || { rm -f "$gecici"; return 1; }
  chmod 600 "$gecici" "$sifre_dosyasi"
  # ⚠️ Parola komut satırına YAZILMAZ (`ps` ile görünürdü) → dosyadan okunur.
  printf '%s' "$AYAR_SIFRE" > "$sifre_dosyasi"

  local yollar=() p
  for p in $AYAR_YOLLARI; do [ -e "$REPO_DIZINI/$p" ] && yollar+=("$p"); done
  if [ "${#yollar[@]}" -eq 0 ]; then
    uyari "ayar dosyası bulunamadı ($REPO_DIZINI) → atlandı"
    rm -f "$gecici" "$sifre_dosyasi"; return 0
  fi

  if ! tar czf - -C "$REPO_DIZINI" "${yollar[@]}" 2>/dev/null \
       | gpg --batch --yes --quiet --symmetric --cipher-algo AES256 \
             --passphrase-file "$sifre_dosyasi" -o "$gecici" 2>/dev/null; then
    hata "ayar yedeği şifrelenemedi"; rm -f "$gecici" "$sifre_dosyasi"; return 1
  fi

  # ⚠️ ŞİFRE ÇÖZÜLEBİLİRLİĞİ HEMEN DOĞRULANIR. Açılamayan şifreli bir yedek,
  # yedeği olmamaktan kötüdür (var sanılır). Aynı ilke DB dump'ında da var.
  local icerik
  icerik=$(gpg --batch --quiet --decrypt --passphrase-file "$sifre_dosyasi" "$gecici" 2>/dev/null | tar tzf - 2>/dev/null | wc -l)
  rm -f "$sifre_dosyasi"
  if [ "${icerik:-0}" -lt 3 ]; then
    hata "ayar yedeği çözülemedi ($icerik girdi) — yüklenmedi"; rm -f "$gecici"; return 1
  fi

  local A; curl_args A
  if curl "${A[@]}" -T "$gecici" "$(ftp_tabani)$ad" >/dev/null 2>&1; then
    oldu "ayar yedeği yüklendi ve çözülebilirliği doğrulandı ($ad, $icerik dosya)"
  else
    hata "ayar yedeği yüklenemedi"; rm -f "$gecici"; return 1
  fi
  rm -f "$gecici"

  # Kendi saklama kuralı — ⚠️ DB yedeğinin kalıbıyla çakışmaz (`-ayar_` öneki).
  local liste; liste=$(curl "${A[@]}" --list-only "$(ftp_tabani)" 2>/dev/null \
    | tr -d '\r' | grep -E "^${ONEK}-ayar_[0-9]{8}_[0-9]{4}\.tar\.gz\.gpg$" | sort)
  local koru; koru=$(printf '%s\n' "$liste" | tail -n "$AYAR_SAKLA")
  local d
  while read -r d; do
    [ -z "$d" ] && continue
    printf '%s\n' "$koru" | grep -qxF "$d" && continue
    curl "${A[@]}" -Q "DELE /${UZAK_DIZIN#/}/$d" "$(ftp_tabani)" >/dev/null 2>&1
  done <<< "$liste"
}

# Şifreli ayar yedeğini çözer — kurtarma anında kullanılır.
#   scripts/yedek_ftp.sh ayar-coz [dosya-adi]   (boşsa FTP'deki en yenisi)
mod_ayar_coz() {
  local ad="${1:-}" A; curl_args A
  if [ -z "$ad" ]; then
    ad=$(curl "${A[@]}" --list-only "$(ftp_tabani)" 2>/dev/null | tr -d '\r' \
         | grep -E "^${ONEK}-ayar_[0-9]{8}_[0-9]{4}\.tar\.gz\.gpg$" | sort | tail -1)
  fi
  [ -n "$ad" ] || { hata "FTP'de ayar yedeği bulunamadı"; return 1; }
  [ -n "${AYAR_SIFRE:-}" ] || { hata "AYAR_SIFRE tanımsız — parola olmadan çözülemez"; return 1; }
  local hedef="$YEREL_DIZIN/coz-$(date +%s)" sd; sd=$(mktemp); chmod 600 "$sd"
  printf '%s' "$AYAR_SIFRE" > "$sd"
  mkdir -p "$hedef"
  if curl "${A[@]}" "$(ftp_tabani)$ad" 2>/dev/null \
     | gpg --batch --quiet --decrypt --passphrase-file "$sd" 2>/dev/null \
     | tar xzf - -C "$hedef" 2>/dev/null; then
    rm -f "$sd"
    oldu "çözüldü → $hedef"
    find "$hedef" -type f | sed 's|^|    |'
  else
    rm -f "$sd"; rm -rf "$hedef"; hata "çözme başarısız ($ad)"; return 1
  fi
}

# ── test ─────────────────────────────────────────────────────────────────────
mod_test() {
  local A; curl_args A
  kayit "FTP bağlantısı deneniyor ($(ftp_tabani | sed 's|//[^@]*@|//|'))  FTPS=$FTPS"
  if curl "${A[@]}" --list-only "$(ftp_tabani)" >/dev/null 2>&1; then
    oldu "dizin okunabiliyor"
  else
    uyari "dizin yok ya da boş — yükleme sırasında oluşturulacak"
  fi
  local gecici="/tmp/.yedek_test_$$"; echo "ihaletakip yedek testi $(date)" > "$gecici"
  if curl "${A[@]}" -T "$gecici" "$(ftp_tabani)_test.txt" >/dev/null 2>&1; then
    oldu "yazma yetkisi var"
    curl "${A[@]}" -Q "DELE /${UZAK_DIZIN#/}/_test.txt" "$(ftp_tabani)" >/dev/null 2>&1 \
      && oldu "silme yetkisi var (uzak temizlik çalışacak)" || uyari "SİLME YETKİSİ YOK → uzak temizlik yapılamaz, alan dolar"
  else
    hata "yazma başarısız — kullanıcı/şifre/host ya da TLS sorunu"; rm -f "$gecici"; return 1
  fi
  rm -f "$gecici"
  docker exec "$DB_KONTEYNER" pg_dump --version >/dev/null 2>&1 \
    && oldu "pg_dump erişilebilir ($DB_KONTEYNER)" || { hata "pg_dump çalıştırılamadı"; return 1; }
  if [ "$AYAR_YEDEK" = "1" ]; then
    command -v gpg >/dev/null 2>&1 || { hata "gpg kurulu değil — ayar yedeği alınamaz"; return 1; }
    [ -n "${AYAR_SIFRE:-}" ] && oldu "ayar yedeği için parola tanımlı" \
      || uyari "AYAR_SIFRE tanımsız → sırlar yedeklenmeyecek"
  fi
}

# ── al ───────────────────────────────────────────────────────────────────────
mod_al() {
  local ad dosya basla bitti boyut uzak_boyut
  ad="${ONEK}_$(date +%Y%m%d_%H%M).dump"
  dosya="$YEREL_DIZIN/$ad"
  mkdir -p "$YEREL_DIZIN"

  kayit "yedek alınıyor → $ad"
  basla=$(date +%s)
  # ⚠️ `docker exec`, `docker compose exec` DEĞİL: compose eklentisi disk/yük
  # baskısı altında yüklenemiyor ("unknown shorthand flag: 'T'", üretimde görüldü).
  if ! docker exec "$DB_KONTEYNER" pg_dump -U "$DB_KULLANICI" -d "$DB_ADI" \
        -Fc -Z"$SIKISTIRMA" --no-owner --no-privileges > "$dosya" 2>"$dosya.err"; then
    hata "pg_dump başarısız: $(head -c 300 "$dosya.err" 2>/dev/null)"
    rm -f "$dosya"; durum_yaz "HATA" "pg_dump başarısız"; return 1
  fi
  bitti=$(date +%s)
  boyut=$(stat -c%s "$dosya" 2>/dev/null || echo 0)
  rm -f "$dosya.err"
  oldu "dump bitti: $(numfmt --to=iec "$boyut" 2>/dev/null || echo "$boyut B") / $(( (bitti-basla)/60 )) dk $(( (bitti-basla)%60 )) sn"

  # ⚠️ DOĞRULAMA YÜKLEMEDEN ÖNCE. tinyfect `gzip -t` ile aynı şeyi yapıyor ve bu
  # doğru bir alışkanlık: bozuk bir yedeği FTP'ye atmak, sağlam sanılan bir
  # yedekle günlerce yaşamak demektir. `-Fc` için karşılığı `pg_restore -l`.
  if [ "$boyut" -lt 1048576 ]; then
    hata "dump şüpheli küçük ($boyut B) — yüklenmedi"; durum_yaz "HATA" "dump çok küçük"; return 1
  fi
  local girdi
  girdi=$(docker exec -i "$DB_KONTEYNER" pg_restore -l < "$dosya" 2>/dev/null | grep -c '^[0-9;]' || true)
  if [ "${girdi:-0}" -lt 50 ]; then
    hata "pg_restore -l içindekiler listesi okunamadı ($girdi girdi) — dump BOZUK, yüklenmedi"
    durum_yaz "HATA" "dump doğrulaması başarısız"; return 1
  fi
  oldu "dump doğrulandı ($girdi nesne)"

  # ── yer kontrolü (yüklemeden ÖNCE) ──
  if ! yer_var_mi "$boyut"; then
    hata "FTP'de yer olmadığı için yüklenmedi — yerel kopya duruyor: $dosya"
    durum_yaz "HATA" "FTP kotası dolu"; return 1
  fi

  # ── yükleme (yeniden denemeli) ──
  local A; curl_args A
  local i=1 yuklendi=0
  while [ "$i" -le "$YUKLEME_DENEME" ]; do
    if curl "${A[@]}" -T "$dosya" "$(ftp_tabani)$ad" >/dev/null 2>&1; then yuklendi=1; break; fi
    uyari "yükleme denemesi $i/$YUKLEME_DENEME başarısız"; i=$((i+1)); sleep $((i*10))
  done
  if [ "$yuklendi" -ne 1 ]; then
    hata "FTP yüklemesi başarısız — yerel kopya duruyor: $dosya"
    durum_yaz "HATA" "FTP yüklemesi başarısız (yerel kopya var)"; return 1
  fi

  # ⚠️ YÜKLENEN BOYUT DOĞRULANIR. FTP yüklemesi yarıda kesilince curl bazen
  # başarı döndürür; uzak dosya sessizce KISA kalır ve kimse fark etmez.
  uzak_boyut=$(curl "${A[@]}" --head "$(ftp_tabani)$ad" 2>/dev/null | awk '/Content-Length|content-length/ {print $2}' | tr -d '\r')
  if [ -n "$uzak_boyut" ] && [ "$uzak_boyut" != "$boyut" ]; then
    hata "uzak boyut eşleşmiyor (yerel=$boyut uzak=$uzak_boyut) — yedek GÜVENİLMEZ"
    durum_yaz "HATA" "uzak boyut eşleşmiyor"; return 1
  fi
  oldu "FTP'ye yüklendi ve boyut doğrulandı ($ad)"

  # ⚠️ Ayar yedeği DB yedeğinden SONRA ve kendi başarısı DB'yi etkilemeyecek
  # şekilde alınır: sırlar yüklenemezse bu bir uyarıdır, ama veritabanı yedeği
  # yine geçerlidir ve "HATA" yazmak gerçek arızayı gölgelerdi.
  ayar_yedegi || uyari "ayar yedeği alınamadı (veritabanı yedeği geçerli)"

  mod_temizle
  find "$YEREL_DIZIN" -name "${ONEK}_*.dump" -mtime "+$YEREL_SAKLA_GUN" -delete 2>/dev/null
  durum_yaz "TAMAM" "$ad · $(numfmt --to=iec "$boyut" 2>/dev/null || echo "$boyut B") · $(( (bitti-basla)/60 )) dk"
  kayit "bitti: $ad"
}

# ── saatlik ──────────────────────────────────────────────────────────────────
# Kullanıcı verisi yedeği (gerekçe yukarıda, SAATLIK_* ayarları). Gecelik `al`dan bağımsız
# kilit: 04:00 tam dump'ı sürerken de çalışabilir (ikisi de salt okunur anlık görüntü).
mod_saatlik() {
  local ad dosya basla bitti boyut uzak_boyut dizin="$YEREL_DIZIN/saatlik"
  ad="${ONEK}_saatlik_$(date +%Y%m%d_%H%M).dump"
  dosya="$dizin/$ad"
  mkdir -p "$dizin"

  local haric=() k
  for k in $SAATLIK_HARIC; do haric+=("--exclude-table-data=$k"); done

  kayit "saatlik yedek → $ad (veri hariç: $SAATLIK_HARIC)"
  basla=$(date +%s)
  if ! docker exec "$DB_KONTEYNER" pg_dump -U "$DB_KULLANICI" -d "$DB_ADI" \
        -Fc -Z"$SIKISTIRMA" --no-owner --no-privileges "${haric[@]}" > "$dosya" 2>"$dosya.err"; then
    hata "pg_dump başarısız: $(head -c 300 "$dosya.err" 2>/dev/null)"
    rm -f "$dosya" "$dosya.err"; [ "$KURU" = 1 ] || saatlik_durum_yaz "HATA" "pg_dump başarısız"; return 1
  fi
  bitti=$(date +%s)
  rm -f "$dosya.err"
  boyut=$(stat -c%s "$dosya" 2>/dev/null || echo 0)

  # Doğrulama YÜKLEMEDEN ÖNCE: boyut + içindekiler + kullanıcı tablosunun verisi gerçekten içeride mi.
  if [ "$boyut" -lt "$SAATLIK_MIN_BAYT" ]; then
    hata "dump şüpheli küçük ($boyut B) — yüklenmedi"
    [ "$KURU" = 1 ] || saatlik_durum_yaz "HATA" "dump çok küçük"; return 1
  fi
  local toc girdi
  toc=$(docker exec -i "$DB_KONTEYNER" pg_restore -l < "$dosya" 2>/dev/null)
  girdi=$(printf '%s\n' "$toc" | grep -c '^[0-9;]' || true)
  if [ "${girdi:-0}" -lt 50 ] || ! printf '%s\n' "$toc" | grep -q "TABLE DATA public accounts_user "; then
    hata "dump doğrulanamadı ($girdi girdi / accounts_user verisi yok) — yüklenmedi"
    [ "$KURU" = 1 ] || saatlik_durum_yaz "HATA" "dump doğrulaması başarısız"; return 1
  fi
  if printf '%s\n' "$toc" | grep -q "TABLE DATA public ekap_"; then
    hata "dump EKAP verisi içeriyor (hariç tutma çalışmadı) — yüklenmedi"
    [ "$KURU" = 1 ] || saatlik_durum_yaz "HATA" "hariç tutma çalışmadı"; return 1
  fi
  oldu "dump: $(numfmt --to=iec "$boyut" 2>/dev/null || echo "$boyut B") / $((bitti-basla)) sn / $girdi nesne"

  if [ "$KURU" = 1 ]; then
    oldu "KURU: yükleme ve silme yapılmadı (yerel: $dosya)"; return 0
  fi

  if ! yer_var_mi "$boyut"; then
    hata "FTP'de yer yok — yerel kopya duruyor: $dosya"; saatlik_durum_yaz "HATA" "FTP kotası dolu"; return 1
  fi

  local A; curl_args A
  local hedef="$(ftp_tabani)${SAATLIK_DIZIN}/$ad" i=1 yuklendi=0
  while [ "$i" -le "$YUKLEME_DENEME" ]; do
    if curl "${A[@]}" -T "$dosya" "$hedef" >/dev/null 2>&1; then yuklendi=1; break; fi
    uyari "yükleme denemesi $i/$YUKLEME_DENEME başarısız"; i=$((i+1)); sleep $((i*5))
  done
  if [ "$yuklendi" -ne 1 ]; then
    hata "FTP yüklemesi başarısız — yerel kopya duruyor: $dosya"
    saatlik_durum_yaz "HATA" "FTP yüklemesi başarısız (yerel kopya var)"; return 1
  fi
  uzak_boyut=$(curl "${A[@]}" --head "$hedef" 2>/dev/null | awk '/Content-Length|content-length/ {print $2}' | tr -d '\r')
  if [ "$uzak_boyut" != "$boyut" ]; then
    hata "uzak boyut eşleşmiyor (yerel=$boyut uzak=${uzak_boyut:-?}) — yedek GÜVENİLMEZ"
    saatlik_durum_yaz "HATA" "uzak boyut eşleşmiyor"; return 1
  fi
  oldu "yüklendi ve boyut doğrulandı (${SAATLIK_DIZIN}/$ad)"

  # Saklama — ⚠️ yalnızca KATI ad kalıbı; saatlik dizindeki başka hiçbir dosyaya dokunulmaz.
  local liste n=0 d
  liste=$(curl "${A[@]}" --list-only "$(ftp_tabani)${SAATLIK_DIZIN}/" 2>/dev/null | tr -d '\r' \
    | grep -E "^${ONEK}_saatlik_[0-9]{8}_[0-9]{4}\.dump$" | sort)
  while read -r d; do
    [ -z "$d" ] && continue
    if curl "${A[@]}" -Q "DELE /${UZAK_DIZIN#/}/${SAATLIK_DIZIN}/$d" "$(ftp_tabani)" >/dev/null 2>&1; then
      n=$((n+1))
    else
      uyari "silinemedi: ${SAATLIK_DIZIN}/$d"
    fi
  done < <(printf '%s\n' "$liste" | head -n -"$SAATLIK_SAKLA")
  [ "$n" -gt 0 ] && oldu "uzakta $n eski saatlik yedek silindi"
  ls -1t "$dizin"/${ONEK}_saatlik_*.dump 2>/dev/null | tail -n +"$((SAATLIK_YEREL_SAKLA + 1))" | xargs -r rm -f

  saatlik_durum_yaz "TAMAM" "$ad · $(numfmt --to=iec "$boyut" 2>/dev/null || echo "$boyut B") · $((bitti-basla)) sn"
}

# ── temizle ──────────────────────────────────────────────────────────────────
# Saklama kuralı: son GUNLUK_SAKLA günlük + ayın 1'ine ait son AYLIK_SAKLA dosya.
# ⚠️⚠️ SİLME YALNIZCA KENDİ AD KALIBIMIZA UYAN DOSYALARA UYGULANIR
# (`^ihaletakip_YYYYMMDD_HHMM\.dump$`). tinyfect'in `spark_*` dosyaları ve
# tanımadığımız hiçbir şey ASLA silinmez — yanlış silme geri alınamaz.
mod_temizle() {
  local A; curl_args A
  local liste; liste=$(curl "${A[@]}" --list-only "$(ftp_tabani)" 2>/dev/null \
    | tr -d '\r' | grep -E "^${ONEK}_[0-9]{8}_[0-9]{4}\.dump$" | sort)
  [ -z "$liste" ] && { oldu "uzakta temizlenecek dosya yok"; return 0; }

  local korunacak silinecek=0
  korunacak=$(printf '%s\n' "$liste" | tail -n "$GUNLUK_SAKLA")
  # Ayın 1'ine ait olanların son N'i (uzun dönem arşiv)
  korunacak="$korunacak
$(printf '%s\n' "$liste" | grep -E "^${ONEK}_[0-9]{6}01_" | tail -n "$AYLIK_SAKLA")"

  while read -r d; do
    [ -z "$d" ] && continue
    printf '%s\n' "$korunacak" | grep -qxF "$d" && continue
    if curl "${A[@]}" -Q "DELE /${UZAK_DIZIN#/}/$d" "$(ftp_tabani)" >/dev/null 2>&1; then
      silinecek=$((silinecek+1))
    else
      uyari "silinemedi: $d"
    fi
  done <<< "$liste"
  [ "$silinecek" -gt 0 ] && oldu "uzakta $silinecek eski yedek silindi" || oldu "uzak saklama kuralı zaten uygun"
}

# ── liste / durum ────────────────────────────────────────────────────────────
mod_liste() {
  local A; curl_args A
  kayit "FTP'deki yedeklerimiz:"
  curl "${A[@]}" "$(ftp_tabani)" 2>/dev/null | tr -d '\r' | grep -E "${ONEK}_" \
    | awk '{printf "  %10.2f MB  %s %s %s  %s\n", $5/1048576, $6,$7,$8, $9}' || echo "  (yok)"
  kayit "saatlik (son 6):"
  curl "${A[@]}" "$(ftp_tabani)${SAATLIK_DIZIN}/" 2>/dev/null | tr -d '\r' | grep -E "${ONEK}_saatlik_" \
    | sort -k9 | tail -6 | awk '{printf "  %10.2f MB  %s %s %s  %s\n", $5/1048576, $6,$7,$8, $9}' || echo "  (yok)"
}

mod_durum() {
  if [ -r "$YEREL_DIZIN/son-durum.txt" ]; then
    IFS='|' read -r zaman sonuc mesaj < "$YEREL_DIZIN/son-durum.txt"
    printf '  son çalışma : %s\n  sonuç       : %s\n  ayrıntı     : %s\n' "$zaman" "$sonuc" "$mesaj"
    # ⚠️ Tazelik kontrolü: "TAMAM" yazması yetmez, o TAMAM'ın ne zaman yazıldığı
    # önemli — cron'un sessizce durması en sık rastlanan yedek arızasıdır.
    local yas; yas=$(( ( $(date +%s) - $(stat -c%Y "$YEREL_DIZIN/son-durum.txt") ) / 3600 ))
    if [ "$yas" -gt 30 ]; then printf '  ⚠ TAZELİK   : %s saat önce — yedek DURMUŞ olabilir\n' "$yas"
    else printf '  tazelik     : %s saat önce (iyi)\n' "$yas"; fi
  else
    printf '  (henüz hiç yedek alınmamış)\n'
  fi
  printf '  yerel kopyalar:\n'
  ls -lah "$YEREL_DIZIN"/${ONEK}_*.dump 2>/dev/null | awk '{printf "    %s  %s %s %s\n", $5,$6,$7,$8}' || printf '    (yok)\n'
  if [ -r "$YEREL_DIZIN/son-saatlik.txt" ]; then
    IFS='|' read -r zaman sonuc mesaj < "$YEREL_DIZIN/son-saatlik.txt"
    local syas; syas=$(( ( $(date +%s) - $(stat -c%Y "$YEREL_DIZIN/son-saatlik.txt") ) / 60 ))
    printf '  saatlik     : %s — %s (%s dk önce)\n' "$sonuc" "$mesaj" "$syas"
    [ "$syas" -gt 120 ] && printf '  ⚠ SAATLİK yedek %s dakikadır yazılmadı — cron DURMUŞ olabilir\n' "$syas"
  else
    printf '  saatlik     : (henüz çalışmadı)\n'
  fi
  # ⚠️ Kota kullanımı durumun parçasıdır: alan dolmaya başladığında bunu yedek
  # BAŞARISIZ olduğu gün değil, öncesinde görmek gerekir.
  local k; k=$(ftp_kullanim_bayt 2>/dev/null)
  if [ -n "$k" ] && [ "$k" -gt 0 ] 2>/dev/null; then
    printf '  FTP kullanımı : %s / %sG  (%%%d)\n' \
      "$(numfmt --to=iec "$k" 2>/dev/null || echo "$k")" "$FTP_KOTA_GB" \
      "$(( k * 100 / (FTP_KOTA_GB * 1073741824) ))"
  fi
}

MOD="${1:-durum}"
mkdir -p "$(dirname "$LOG")" "$YEREL_DIZIN" 2>/dev/null
case "$MOD" in
  test)    mod_test ;;
  durum)   mod_durum ;;
  liste)   mod_liste ;;
  # ⚠️ flock: cron gecikirse ya da elle tetiklenirse iki dump üst üste binmesin
  # (ikisi birlikte hem disk hem uzun snapshot demek).
  al)      exec 9>"$KILIT"; flock -n 9 || { kayit "başka bir yedek çalışıyor — atlandı"; exit 0; }; mod_al ;;
  # ⚠️ Saatlik ayrı kilit + ayrı log: gecelik tam dump sürerken de koşar, saatte bir satır
  # gecelik logu boğmasın.
  saatlik) LOG="$LOG_SAATLIK"; exec 8>"$KILIT_SAATLIK"
           flock -n 8 || { kayit "önceki saatlik yedek sürüyor — atlandı"; exit 0; }; mod_saatlik ;;
  temizle) exec 9>"$KILIT"; flock -n 9 || { kayit "kilit meşgul"; exit 0; }; mod_temizle ;;
  # ⚠️ Ayar yedeğini TEK BAŞINA alabilmek şart: aksi hâlde onu test etmek için
  # 14 dakikalık bir veritabanı dump'ı beklemek gerekirdi.
  ayar)    exec 9>"$KILIT"; flock -n 9 || { kayit "kilit meşgul"; exit 0; }; ayar_yedegi ;;
  ayar-coz) mod_ayar_coz "${2:-}" ;;
  *) echo "kullanım: $0 [test|al|saatlik|durum|liste|temizle|ayar|ayar-coz [dosya]]" >&2; exit 2 ;;
esac
