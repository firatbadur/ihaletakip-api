# ═══════════════════════════════════════════════════════
#  IhaleTakip API — Python 3.13 slim (Ubuntu tabanlı)
# ═══════════════════════════════════════════════════════
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Sistem bağımlılıkları (psycopg, cryptography, docx/pdf build gereksinimleri)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bağımlılıkları önce kopyala (katman cache)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Proje kodu
COPY . .

# Entrypoint çalıştırılabilir yap
RUN chmod +x /app/docker/entrypoint.sh

# Root olmayan kullanıcı
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "180"]
