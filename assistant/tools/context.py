"""
Araç çalıştırma bağlamı.

Her araç `calistir(ctx, **params)` imzasıyla çağrılır ve ASLA exception sızdırmaz;
hata `{"ok": False, "hata": "<Türkçe>"}` olarak döner (bkz. `assistant/services/agent.py`).

⚠️ Araç katmanında `accounts.premium.require_premium` **çağrılmaz**: o bir DRF
`APIException`'dır ve Celery görevi içinde 403'e dönüşmez, görevi çökertir. Premium
kontrolü `ctx.premium` bayrağı ile yapılır ve `{"ok": False, "kilitli": True}` döner —
model bunu kullanıcıya "bu Pro özelliği" cümlesine çevirir.
"""
from dataclasses import dataclass, field


@dataclass
class ToolContext:
    """Bir sohbet turu boyunca yaşayan araç bağlamı."""

    user: object
    premium: bool = False
    conversation: object = None

    # İhale kartı havuzu: İKN → kart sözlüğü. Araçlar buraya YAZAR, model yalnızca
    # buradaki İKN'leri kart olarak gösterebilir → uydurma İKN mümkün değil.
    # (Mevcut `assistant/tasks.py` güvenlik ağının araç dünyasındaki karşılığı.)
    card_pool: dict = field(default_factory=dict)

    # Araç sonuçlarının bu turda harcadığı yaklaşık token — döngü bütçe kapısı okur.
    harcanan_token: int = 0

    def kart_ekle(self, tender) -> dict:
        """Tender'ı karta çevirip havuza yazar ve kartı döner."""
        from assistant.services.matching import tender_card

        kart = tender_card(tender)
        if kart.get("ikn"):
            self.card_pool[kart["ikn"]] = kart
        return kart
