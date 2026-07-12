#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════
#  Container başlangıç script'i
#  - migrate  → collectstatic → CMD çalıştır
#  (admin BİLEREK otomatik oluşturulmaz; parolayı ezmemek için elle: create_admin)
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

  # DEBUG=False'ta admin, manifest'li statik dosyalara bağımlıdır. collectstatic
  # sessizce başarısız olursa (izin/volume sorunu) tüm admin 500 döner — bu yüzden
  # `|| true` YOK: hata varsa konteyner ayağa kalkmasın, sebebi logda görünsün.
  if [ ! -w /app/staticfiles ]; then
    echo "❌ /app/staticfiles yazılabilir değil."
    echo "   Named volume root sahipli oluşmuş olabilir. Çözüm:"
    echo "     docker compose down && docker volume rm \$(basename \$PWD)_static_volume"
    echo "     docker compose up -d --build"
    exit 1
  fi

  echo "🎨 Statik dosyalar toplanıyor..."
  python manage.py collectstatic --noinput

  # NOT: create_admin BİLEREK burada çağrılmaz. Her başlangıçta çalışırsa
  # admin parolasını env'deki değere geri yazar → elle değiştirilen parola
  # her restart'ta kaybolur. İlk kurulumda admini elle oluşturun:
  #   docker compose exec web python manage.py create_admin
fi

echo "🚀 Servis başlatılıyor: $*"
exec "$@"
