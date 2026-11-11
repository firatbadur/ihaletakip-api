# ═══════════════════════════════════════════════════════
#  IhaleTakip API — Python 3.13 slim (Debian 13 "trixie" tabanlı)
# ═══════════════════════════════════════════════════════
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Sistem bağımlılıkları (psycopg, cryptography, docx/pdf build gereksinimleri,
# tesseract: ekap/tools/ocr.py — Türkçe dil paketiyle birlikte,
# antiword/catdoc: asistan eklerinde eski OLE .doc → metin; assistant/services/attachments.py.
# İkisi de trixie main'de var — doğrulandı 2026-11-11: antiword 0.37-17, catdoc 1:0.95-6)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        tesseract-ocr \
        tesseract-ocr-tur \
        antiword \
        catdoc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bağımlılıkları önce kopyala (katman cache)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Proje kodu
COPY . .

# Entrypoint çalıştırılabilir yap
RUN chmod +x /app/docker/entrypoint.sh

# staticfiles/ ve media/ named volume olarak bağlanıyor. Mount noktası image'da
# yoksa Docker onu root:root yaratır, appuser collectstatic'i yazamaz →
# manifest üretilmez → admin sayfaları 500 döner. Dizinleri önden oluştur ki
# volume ilk yaratılışta appuser sahipliğini devralsın. `ek_depo`: asistan eklerinin
# ham dosyaları (named volume `assistant_ek`; MEDIA_ROOT DIŞI — /media/blog/ açık sunulur).
RUN mkdir -p /app/staticfiles /app/media /app/ek_depo

# Root olmayan kullanıcı
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# 4 çekirdek / 8 GB. `gthread`: istekler ağırlıklı olarak DB'yi BEKLER (CPU harcamaz);
# sync worker'da bekleyen bir istek tüm worker'ı bloklar, thread'lerde bloklamaz.
# 4×2 = 8 eşzamanlı istek, kalıcı bağlantıyla (CONN_MAX_AGE) ~8 PG bağlantısı —
# celery'ninkilerle birlikte max_connections=100'ün çok altında.
# `--max-requests` + jitter: worker'ları periyodik geri dönüştürüp bellek sızıntısını
# sınırlar (jitter, hepsinin aynı anda dönmesini önler).
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "4", "--threads", "2", "--worker-class", "gthread", \
     "--max-requests", "1000", "--max-requests-jitter", "100", "--timeout", "180"]
