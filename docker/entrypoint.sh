#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════
#  Container başlangıç script'i
#  - migrate  → collectstatic → admin oluştur → CMD çalıştır
#  Sadece "web" servisinde migrate çalışsın diye RUN_MIGRATIONS
#  bayrağı kullanılır (worker/beat migrate çalıştırmaz).
# ═══════════════════════════════════════════════════════
set -e

echo "⏳ Veritabanı bekleniyor..."
python - <<'PY'
import os, time, sys
import psycopg
url = os.environ.get("DATABASE_URL", "")
if not url.startswith("postgres"):
    print("SQLite/başka DB — bekleme atlandı.")
    sys.exit(0)
for i in range(30):
    try:
        psycopg.connect(url, connect_timeout=2).close()
        print("✅ Veritabanı hazır.")
        break
    except Exception as e:
        print(f"  ...deneme {i+1}/30: {e}")
        time.sleep(2)
else:
    print("❌ Veritabanına bağlanılamadı.")
    sys.exit(1)
PY

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "⚙️  Migration'lar uygulanıyor..."
  python manage.py migrate --noinput

  echo "🎨 Statik dosyalar toplanıyor..."
  python manage.py collectstatic --noinput || true

  echo "👤 Admin kullanıcı kontrol ediliyor..."
  python manage.py create_admin || true
fi

echo "🚀 Servis başlatılıyor: $*"
exec "$@"
