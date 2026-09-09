# EKAP insan doğrulaması oturumunu ayakta tutan tarayıcı servisi.
#
# ⚠️ Ayrı imaj: Chromium + bağımlılıkları ~1 GB tutar; ana `web`/worker imajını
# bu kadar şişirmenin anlamı yok (onlar tarayıcı çalıştırmıyor).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
# ⚠️ `playwright` Python paketi imajda KURULU DEĞİL (yalnızca tarayıcı ikilileri
# /ms-playwright altında hazır gelir) → açıkça kurulur. Sürüm imaj etiketiyle
# BİREBİR aynı olmalı, aksi hâlde paket indirilmemiş bir tarayıcı sürümü arar.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir playwright==1.47.0

# ⚠️ Turnstile bu tarayıcıya ETKİLEŞİMLİ kutu gösteriyor (ölçüldü, ekran
# görüntüsüyle doğrulandı) → kutuyu bir insanın tıklayabilmesi için sanal ekran
# + VNC + noVNC gerekir. Otomatik tıklama YAPILMAZ.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Kalıcı tarayıcı profili (Turnstile yerleşik oturumları sessiz geçiriyor →
# her turda temiz profil açmak her turda etkileşimli kutu demek olurdu).
RUN mkdir -p /app/.browser && chmod 777 /app/.browser \
    && chmod +x /app/docker/browser-entrypoint.sh

ENTRYPOINT ["/app/docker/browser-entrypoint.sh"]
