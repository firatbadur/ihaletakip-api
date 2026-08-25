"""
Araç sonucu kırpma yardımcıları.

Neden sert bir tavan var: her araç sonucu, sohbetin GERİ KALAN her turunda yeniden
gönderilir (Messages API durumsuzdur). Kırpılmamış tek bir `dagilim` bloğu üç turluk
bir sohbette üç kez ödenir. Tavan aşılırsa liste kuyruğu atılır ve sonuca
`"kirpildi": True` konur — model eksikliği kullanıcıya söyleyebilsin.

⚠️ Para alanları STRING'dir (`"2834670.00"`). `float()` ile bozma; Decimal → str.
"""
import json

# Araç başına yaklaşık token tavanı (~1 token ≈ 3 karakter Türkçe JSON'da).
AZAMI_TOKEN = 2500
AZAMI_KARAKTER = AZAMI_TOKEN * 3


def para(v):
    """Decimal/str para değerini stringe çevirir; None ise None bırakır."""
    return None if v is None else str(v)


def kes(metin, n=200):
    """Uzun metni n karakterde keser (ihale adları 500 karaktere kadar olabiliyor)."""
    if not metin:
        return ""
    metin = str(metin)
    return metin if len(metin) <= n else metin[: n - 1] + "…"


def token_tahmin(sonuc) -> int:
    return len(json.dumps(sonuc, ensure_ascii=False, default=str)) // 3


def butceye_sigdir(sonuc: dict, liste_anahtari: str = "liste") -> dict:
    """
    Sonuç tavanı aşıyorsa `liste_anahtari` altındaki listenin kuyruğunu atar.

    Liste boşalana kadar yarılayarak küçültür; hâlâ büyükse liste tamamen boşaltılır
    (üst düzey sayılar — `toplam` gibi — korunur, çünkü modelin "kaç sonuç var"
    bilgisini kaybetmesi listeyi kaybetmesinden daha kötüdür).
    """
    liste = sonuc.get(liste_anahtari)
    if not isinstance(liste, list):
        return sonuc
    while liste and len(json.dumps(sonuc, ensure_ascii=False, default=str)) > AZAMI_KARAKTER:
        liste = liste[: max(1, len(liste) // 2)] if len(liste) > 1 else []
        sonuc[liste_anahtari] = liste
        sonuc["kirpildi"] = True
    return sonuc
