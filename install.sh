#!/usr/bin/env bash
#
# IhaleTakip API — sunucu güncelleme betiği.
#
# Tek komutla: son kodu çeker, konteynerleri yeniden derleyip başlatır, sağlığı doğrular.
# Böylece her seferinde elle `git pull` + `docker compose up` uğraşı olmaz.
#
# Kullanım:
#   ./install.sh            → git pull + build + up + durum + health
#   ./install.sh --logs     → yukarıdakiler + ardından web loglarını izle
#   ./install.sh --migrate  → build/up yerine yalnızca migrate'i worker'da elle çalıştır
#                             (ağır data-migration'da; healthcheck baskısı olmadan)
#
# Notlar:
# - Migration'lar `web` konteyneri açılırken otomatik çalışır (entrypoint). Additif
#   (nullable/default) migration'lar anlıktır. DOLU tabloda uzun süren backfill varsa
#   önce `./install.sh --migrate`, sonra `./install.sh` çalıştır.
# - Beat (DatabaseScheduler) kodda tanımlı yeni periyodik görevleri ancak yeniden
#   başlayınca DB'ye alır; `up -d --build` beat'i de yeniden başlattığı için yeni
#   görevler otomatik devreye girer.
# - `.env.prod`, `credentials/` ve TLS sertifikaları repo'da DEĞİLDİR; sunucuda elle
#   kurulur (bkz. CLAUDE.md "Üretim Dağıtımı"). Bu betik onlara dokunmaz.
#
set -euo pipefail

# Betiğin bulunduğu (repo) dizine geç — nereden çağrılırsa çağrılsın çalışsın.
cd "$(cd "$(dirname "$0")" && pwd)"

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }
err() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

if [ ! -d .git ]; then
  err "Bu dizin bir git deposu değil. Betiği repo kök dizininde çalıştırın."
  exit 1
fi

# ── Yalnızca migrate (ağır data-migration senaryosu) ──
if [ "${1:-}" = "--migrate" ]; then
  say "Migration'lar worker konteynerinde çalıştırılıyor (healthcheck baskısı yok)"
  docker compose exec worker python manage.py migrate
  say "Bitti. Şimdi normal güncelleme için './install.sh' çalıştırabilirsiniz."
  exit 0
fi

say "Son kod çekiliyor (git pull --ff-only)"
if ! git pull --ff-only; then
  err "git pull başarısız — sunucuda yerel değişiklik olabilir."
  err "Çözüm: 'git status' ile bakın; gerekiyorsa 'git stash' ya da 'git reset --hard origin/<dal>'."
  exit 1
fi

say "Konteynerler yeniden derlenip başlatılıyor (docker compose up -d --build)"
docker compose up -d --build

say "Servis durumu"
docker compose ps

say "Sağlık kontrolü (web /health/)"
ok=0
for i in $(seq 1 30); do
  if curl -fsSk https://localhost/health/ >/dev/null 2>&1; then
    echo "  ✓ /health/ → 200"
    ok=1
    break
  fi
  sleep 3
done
if [ "$ok" -ne 1 ]; then
  err "/health/ hâlâ yanıt vermiyor. Log: 'docker compose logs -f web'"
fi

say "Güncelleme tamamlandı."

if [ "${1:-}" = "--logs" ]; then
  say "Web logları (Ctrl-C ile çık)"
  docker compose logs -f web
fi
