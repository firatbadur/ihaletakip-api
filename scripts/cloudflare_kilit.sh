#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# cloudflare_kilit.sh — origin'in 443'ünü YALNIZCA Cloudflare'e açar.
#
#   scripts/cloudflare_kilit.sh durum     # mevcut kuralları göster
#   scripts/cloudflare_kilit.sh uygula    # kilidi kur (idempotent)
#   scripts/cloudflare_kilit.sh tazele    # CF aralıklarını yenile (cron bunu çağırır)
#   scripts/cloudflare_kilit.sh geri-al   # kilidi tamamen kaldır
#
# ⚠️⚠️ NEDEN GEREKLİ: alan adı Cloudflare arkasında (proxied) ama origin IP'si
# 443'te **herkese** açıktı. IP'yi bilen biri doğru `Host` başlığıyla doğrudan
# bağlanıp Cloudflare'in WAF'ını, hız sınırını ve DDoS korumasını **tümüyle
# atlayabiliyordu** (bu oturumda `curl --resolve` ile kanıtlandı). Django'nun
# `ALLOWED_HOSTS` kontrolü yalnızca YANLIŞ Host başlığını eler; doğru başlıkla
# gelen isteği elemez.
#
# ⚠️⚠️ DOCKER YAYINLANAN PORTLAR UFW'Yİ ATLAR → kural `DOCKER-USER` zincirinde
# olmalı. `ufw allow/deny 443` bu portu kısıtlamaz (CLAUDE.md "Üretim Dağıtımı").
#
# ⚠️ `--ctorigdstport` kullanılır, `--dport` DEĞİL: Docker yayınlanan portu
# DNAT'ladığı için paket FORWARD'a geldiğinde hedef port **konteynerin** portuna
# çevrilmiştir. conntrack, isteğin ORİJİNAL hedef portuna bakar → port eşlemesi
# değişse bile kural doğru kalır.
#
# ⚠️ Kurallar `-i eth0` ile sınırlıdır: konteynerler arası trafik (nginx→web),
# loopback (`https://localhost/health/`) ve WireGuard ETKİLENMEZ.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

PORT="${PORT:-443}"
ARAYUZ="${ARAYUZ:-eth0}"
ETIKET="${ETIKET:-cf-kilit-$PORT}"          # iptables comment — kuralları bulmak için
AFTER_RULES="${AFTER_RULES:-/etc/ufw/after.rules}"
BLOK_BAS="# BEGIN $ETIKET"
BLOK_SON="# END $ETIKET"
V4_URL="${V4_URL:-https://www.cloudflare.com/ips-v4}"
# ⚠️ En az kaç aralık beklenir. Cloudflare yıllardır 15 IPv4 aralığı yayımlıyor;
# bu sayı birden 3'e düşerse bu bir CF değişikliği değil, BOZUK BİR ÇEKME demektir
# ve uygulanırsa trafiğin çoğu kesilir. Eşik bilinçli olarak yüksek.
ASGARI_ARALIK="${ASGARI_ARALIK:-10}"

oldu()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
uyari() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
hata()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

[ "$(id -u)" = "0" ] || { hata "root gerekli"; exit 1; }

# ── Cloudflare aralıklarını çek ve DOĞRULA ───────────────────────────────────
# ⚠️⚠️ Doğrulama bu betiğin en kritik parçası: hatalı/boş bir listeyle "yalnızca
# CF'e izin ver" kuralı yazmak, siteyi **tamamen** kapatmak demektir. Bu yüzden
# tek bir bozuk satır bile işi durdurur; kısmen uygulamak yoktur.
cf_araliklari() {
  local ham satir sayi=0
  ham=$(curl -fsS --max-time 25 "$V4_URL" 2>/dev/null | tr -d '\r')
  [ -n "$ham" ] || { hata "CF aralıkları çekilemedi ($V4_URL)"; return 1; }
  while IFS= read -r satir; do
    [ -z "$satir" ] && continue
    if ! printf '%s' "$satir" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$'; then
      hata "geçersiz CIDR: '$satir' → hiçbir şey uygulanmadı"; return 1
    fi
    # Oktet sınırı (regex 999'a izin verir)
    local ip="${satir%%/*}" o
    for o in ${ip//./ }; do
      [ "$o" -le 255 ] || { hata "geçersiz oktet: '$satir'"; return 1; }
    done
    printf '%s\n' "$satir"
    sayi=$((sayi+1))
  done <<< "$ham"
  if [ "$sayi" -lt "$ASGARI_ARALIK" ]; then
    hata "yalnızca $sayi aralık geldi (asgari $ASGARI_ARALIK) → bozuk çekme sayıldı, uygulanmadı"
    return 1
  fi
  return 0
}

# ── Canlı kuralları temizle (etiketli olanlar) ───────────────────────────────
canli_temizle() {
  local n=0
  # ⚠️ Silme ETİKETE göre yapılır: elle eklenmiş ya da başka amaçlı kuralları
  # (ör. 8000 DROP kuralı) kazara silmemek için.
  while iptables -S DOCKER-USER 2>/dev/null | grep -q -- "--comment \"$ETIKET\""; do
    local kural
    kural=$(iptables -S DOCKER-USER | grep -m1 -- "--comment \"$ETIKET\"" | sed 's/^-A DOCKER-USER //')
    # shellcheck disable=SC2086
    iptables -D DOCKER-USER $kural 2>/dev/null || break
    n=$((n+1))
  done
  [ "$n" -gt 0 ] && oldu "$n eski kural kaldırıldı" || true
}

# ── Uygula ───────────────────────────────────────────────────────────────────
mod_uygula() {
  local araliklar
  araliklar=$(cf_araliklari) || return 1
  local sayi; sayi=$(printf '%s\n' "$araliklar" | grep -c .)
  oldu "$sayi Cloudflare aralığı doğrulandı"

  canli_temizle

  # ⚠️ SIRA HAYATİ: önce İZİN kuralları, EN SON drop. Ters sırada yazılsa,
  # drop eklendiği an ile izinlerin tamamlandığı an arasında site kapalı kalırdı.
  local a
  while IFS= read -r a; do
    [ -z "$a" ] && continue
    iptables -A DOCKER-USER -i "$ARAYUZ" -p tcp -m conntrack \
      --ctorigdstport "$PORT" --ctdir ORIGINAL -s "$a" \
      -m comment --comment "$ETIKET" -j RETURN
  done <<< "$araliklar"
  oldu "izin kuralları eklendi"

  iptables -A DOCKER-USER -i "$ARAYUZ" -p tcp -m conntrack \
    --ctorigdstport "$PORT" --ctdir ORIGINAL \
    -m comment --comment "$ETIKET" -j DROP
  oldu "diğer tüm kaynaklar için DROP eklendi"

  kalici_yaz "$araliklar"
}

# ── Kalıcılık ────────────────────────────────────────────────────────────────
# ⚠️ `ufw reload` YAPILMAZ: fail2ban'ın kendi zincirlerini düşürebiliyor
# (CLAUDE.md'de belgelendi). Kurallar canlıya `iptables -A` ile eklenir,
# yeniden başlatma için de after.rules'a yazılır.
kalici_yaz() {
  local araliklar="$1" gecici blok
  gecici=$(mktemp); blok=$(mktemp)
  # Eski bloğu çıkar
  awk -v b="$BLOK_BAS" -v s="$BLOK_SON" '
    $0==b {atla=1} !atla {print} $0==s {atla=0}' "$AFTER_RULES" > "$gecici"
  {
    printf '\n%s (%s)\n' "$BLOK_BAS" "$(date +%Y-%m-%d)"
    cat <<EOF
# Origin'in $PORT portu YALNIZCA Cloudflare aralıklarına açık. Docker yayınlanan
# portlar UFW'yi atladığı için kural DOCKER-USER zincirinde durur.
# Aralıklar $V4_URL adresinden alınır; 'tazele' modu haftalık cron ile yeniler.
# Geri almak: scripts/cloudflare_kilit.sh geri-al
*filter
:DOCKER-USER - [0:0]
EOF
    while IFS= read -r a; do
      [ -z "$a" ] && continue
      printf -- '-A DOCKER-USER -i %s -p tcp -m conntrack --ctorigdstport %s --ctdir ORIGINAL -s %s -m comment --comment "%s" -j RETURN\n' \
        "$ARAYUZ" "$PORT" "$a" "$ETIKET"
    done <<< "$araliklar"
    printf -- '-A DOCKER-USER -i %s -p tcp -m conntrack --ctorigdstport %s --ctdir ORIGINAL -m comment --comment "%s" -j DROP\n' \
      "$ARAYUZ" "$PORT" "$ETIKET"
    printf 'COMMIT\n%s\n' "$BLOK_SON"
  } >> "$gecici"

  # ⚠️⚠️ SÖZDİZİMİ GERÇEKTEN DENENMEDEN YERİNE KONMAZ. Bozuk bir after.rules,
  # sunucu yeniden başladığında UFW'nin **hiç açılmaması** demektir — yani
  # güvenlik için yazdığımız dosya güvenliği tümden kaldırır. Bu yüzden ürettiğimiz
  # blok tek başına ayıklanıp `iptables-restore --test` ile denenir (--test hiçbir
  # şey uygulamaz, yalnızca ayrıştırır).
  awk -v b="$BLOK_BAS" -v s="$BLOK_SON" '
    $0==b {icinde=1; next} $0==s {icinde=0} icinde && !/^#/ {print}' "$gecici" > "$blok"
  if ! iptables-restore --test < "$blok" 2>/dev/null; then
    rm -f "$gecici" "$blok"
    uyari "üretilen blok iptables-restore testini geçemedi → after.rules DEĞİŞTİRİLMEDİ"
    uyari "canlı kurallar aktif ama yeniden başlatmada kaybolur"
    return 1
  fi
  rm -f "$blok"
  cp -a "$AFTER_RULES" "${AFTER_RULES}.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null
  cat "$gecici" > "$AFTER_RULES"     # sahiplik/izinleri koru (mv yerine)
  rm -f "$gecici"
  oldu "kalıcılık yazıldı ($AFTER_RULES, yedek alındı)"
}

# ── Geri al ──────────────────────────────────────────────────────────────────
mod_geri_al() {
  canli_temizle
  if grep -q "$BLOK_BAS" "$AFTER_RULES" 2>/dev/null; then
    cp -a "$AFTER_RULES" "${AFTER_RULES}.bak-$(date +%Y%m%d-%H%M%S)"
    awk -v b="$BLOK_BAS" -v s="$BLOK_SON" '
      $0==b {atla=1} !atla {print} $0==s {atla=0}' "$AFTER_RULES" > "${AFTER_RULES}.yeni"
    mv "${AFTER_RULES}.yeni" "$AFTER_RULES"; chmod 640 "$AFTER_RULES"
    oldu "kalıcı blok da kaldırıldı"
  fi
  oldu "443 yeniden herkese açık (Cloudflare kilidi KAPALI)"
}

# ── Durum ────────────────────────────────────────────────────────────────────
mod_durum() {
  local izin drop
  izin=$(iptables -S DOCKER-USER 2>/dev/null | grep -c -- "--comment \"$ETIKET\".*-j RETURN")
  drop=$(iptables -S DOCKER-USER 2>/dev/null | grep -c -- "--comment \"$ETIKET\".*-j DROP")
  printf '  port %s / %s\n' "$PORT" "$ARAYUZ"
  printf '  izin (RETURN) kuralı : %s\n' "$izin"
  printf '  engel (DROP) kuralı  : %s\n' "$drop"
  if [ "$izin" -gt 0 ] && [ "$drop" -eq 1 ]; then
    oldu "kilit AKTİF — $PORT yalnızca Cloudflare'e açık"
  elif [ "$izin" -eq 0 ] && [ "$drop" -eq 0 ]; then
    uyari "kilit YOK — $PORT herkese açık"
  else
    hata "TUTARSIZ durum (izin=$izin drop=$drop) → 'uygula' ile yeniden kurun"
  fi
  grep -q "$BLOK_BAS" "$AFTER_RULES" 2>/dev/null \
    && oldu "kalıcılık var (yeniden başlatmada korunur)" \
    || uyari "kalıcılık YOK — yeniden başlatmada kilit kaybolur"
  printf '  isabet sayacı:\n'
  iptables -L DOCKER-USER -n -v 2>/dev/null | grep "$ETIKET" | awk '$1!="0"{n++; p+=$1} END{printf "    engellenen paket: %s\n", (p+0)}'
}

case "${1:-durum}" in
  uygula|tazele) mod_uygula ;;
  geri-al)       mod_geri_al ;;
  durum)         mod_durum ;;
  *) echo "kullanım: $0 [uygula|tazele|geri-al|durum]" >&2; exit 2 ;;
esac
