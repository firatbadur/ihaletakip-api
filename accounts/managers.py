"""Özel User manager — email tabanlı oluşturma yardımcıları."""
from django.contrib.auth.models import UserManager as DjangoUserManager


class UserManager(DjangoUserManager):
    """Sosyal giriş için email tabanlı kullanıcı oluşturmayı kolaylaştırır."""

    def get_or_create_social(self, *, email, provider, provider_uid, display_name="", photo_url=""):
        """
        Google/Apple girişinde kullanıcıyı bul veya oluştur.
        Öncelik: provider_uid → email eşleşmesi.
        """
        user = None
        if provider_uid:
            user = self.filter(provider=provider, provider_uid=provider_uid).first()
        if user is None and email:
            user = self.filter(email__iexact=email).first()

        created = False
        if user is None:
            username = self._unique_username(email or f"{provider}_{provider_uid}")
            user = self.create(
                username=username,
                email=email or "",
                display_name=display_name,
                photo_url=photo_url,
                provider=provider,
                provider_uid=provider_uid,
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])
            created = True
        else:
            # Var olan kullanıcıda eksik alanları güncelle
            updates = {}
            if provider_uid and not user.provider_uid:
                updates["provider_uid"] = provider_uid
                updates["provider"] = provider
            if display_name and not user.display_name:
                updates["display_name"] = display_name
            if photo_url and not user.photo_url:
                updates["photo_url"] = photo_url
            if updates:
                for k, v in updates.items():
                    setattr(user, k, v)
                user.save(update_fields=list(updates.keys()))

        return user, created

    def _unique_username(self, base):
        """Email/uid'den benzersiz bir username üret."""
        base = (base or "user").split("@")[0]
        base = "".join(c for c in base if c.isalnum() or c in "._-")[:140] or "user"
        username = base
        i = 1
        while self.filter(username=username).exists():
            username = f"{base}{i}"
            i += 1
        return username
