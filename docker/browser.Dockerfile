# EKAP Turnstile otomatik çözücü servisi (ekap/turnstile.py, ekap_oturum_daemon).
#
# ⚠️ Ayrı imaj: Chromium + bağımlılıkları ~1 GB tutar; ana `web`/worker imajını
# bu kadar şişirmenin anlamı yok (onlar tarayıcı çalıştırmıyor).
# ⚠️ Taban **bookworm** (Debian 12), ana imaj gibi trixie DEĞİL: `patchright install
# --with-deps` Chromium sistem bağımlılıklarını yalnızca desteklediği dağıtımlarda kurar;
# sunucuda bookworm ile ölçüldü (2026-11-13, 2/2 başarı). Python sürümü ana imajla aynı.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ⚠️ `DEBIAN_FRONTEND=noninteractive` ŞART: bu paketler tzdata'yı çekiyor ve tzdata
# postinst'i coğrafi bölge soruyor → build sessizce ASILI KALIR (üretimde yaşandı).
# xvfb: Turnstile headless'ı reddediyor (600010) → görünür tarayıcı sanal ekranda.
# x11vnc + novnc: canlı teşhis için (yalnızca 127.0.0.1, SSH tüneliyle).
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl \
        xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# patchright yalnızca bu imajda: web/worker'lar tarayıcı çalıştırmıyor. Sürüm sabit —
# Chromium ikilisi paket sürümüne bağlı, yükseltme sunucuda yeniden ölçülmeden yapılmaz.
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install patchright==1.64.4 \
    && DEBIAN_FRONTEND=noninteractive patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chmod +x /app/docker/browser-entrypoint.sh

ENTRYPOINT ["/app/docker/browser-entrypoint.sh"]
