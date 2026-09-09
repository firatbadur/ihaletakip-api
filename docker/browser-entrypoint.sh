#!/usr/bin/env bash
# EKAP tarayıcı servisi: sanal ekran + VNC + noVNC + oturum daemon'ı.
#
# ⚠️ Ekran paylaşımı ZORUNLU: Cloudflare Turnstile bu tarayıcıya etkileşimli
# "Gerçek kişi olduğunuzu doğrulayın" kutusu gösteriyor. Kutuyu bir İNSAN
# tıklar; otomatik tıklama yapılmaz. noVNC bu tıkı telefondan/tarayıcıdan
# mümkün kılar.
#
# ⚠️ Port yalnızca 127.0.0.1'e bağlanır (docker-compose) → dışarıya AÇIK DEĞİL,
# erişim SSH tüneliyle olur. Aksi hâlde internete parolasız bir masaüstü
# açmış olurduk.
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
EKRAN="${VNC_EKRAN:-1440x900x24}"

echo "🖥️  Xvfb başlatılıyor ($EKRAN)"
Xvfb "$DISPLAY" -screen 0 "$EKRAN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 2

echo "🔌 x11vnc başlatılıyor (5900)"
x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 \
       -bg >/tmp/x11vnc.log 2>&1

echo "🌐 noVNC başlatılıyor (6080)"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
sleep 1

echo "▶️  oturum daemon'ı başlıyor"
exec python -u manage.py ekap_oturum_daemon --headless false "$@"
