#!/usr/bin/env bash
# EKAP Turnstile çözücü servisi: sanal ekran + VNC/noVNC (teşhis) + çözücü daemon'ı.
#
# ⚠️ Sanal ekran ZORUNLU: Cloudflare headless Chromium'u reddediyor (600010, ölçüldü);
# tarayıcı Xvfb üzerinde GÖRÜNÜR çalışır. noVNC yalnızca canlı teşhis içindir
# (çözücü kendi tıklar); port yalnızca 127.0.0.1'e bağlıdır → erişim SSH tüneliyle:
#   ssh -L 6080:127.0.0.1:6080 tinyfect  →  http://localhost:6080/vnc.html
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
EKRAN="${VNC_EKRAN:-1440x900x24}"

# Konteyner yeniden başlarsa eski kilit dosyası Xvfb'yi "display in use" ile düşürür.
rm -f "/tmp/.X${DISPLAY#:}-lock"

echo "🖥️  Xvfb başlatılıyor ($EKRAN)"
Xvfb "$DISPLAY" -screen 0 "$EKRAN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 2

echo "🔌 x11vnc başlatılıyor (5900)"
x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 \
       -bg >/tmp/x11vnc.log 2>&1 || echo "⚠️ x11vnc başlamadı (çözücü etkilenmez)"

echo "🌐 noVNC başlatılıyor (6080)"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
sleep 1

echo "▶️  Turnstile çözücü başlıyor"
exec python -u manage.py ekap_oturum_daemon "$@"
