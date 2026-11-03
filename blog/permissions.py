"""Blog botu servis anahtarı.

⚠️ `PORTAL_KEY` kullanılmaz: o yalnız hız sınırı kimliğidir, yetki vermez. Yazma
yetkisi ayrı bir anahtarda durur ki sızarsa tek başına iptal edilebilsin.
"""
import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


class BlogBotAnahtari(BasePermission):
    message = "Geçersiz blog botu anahtarı."

    def has_permission(self, request, view):
        beklenen = getattr(settings, "BLOG_BOT_KEY", "") or ""
        gelen = request.META.get("HTTP_X_BLOG_BOT_KEY", "") or ""
        # Anahtar tanımsızsa uç tümüyle kapalı.
        return bool(beklenen) and hmac.compare_digest(gelen.encode(), beklenen.encode())
