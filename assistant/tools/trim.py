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


def kisa_para(v):
    """
    Grafik etiketleri için kısa Türkçe para biçimi: 1.234.567 → "1,2 M".

    ⚠️ Yalnızca GÖRSEL etiket içindir. Metinde ve hesapta tam değer kullanılır;
    burada yuvarlama yapmak sütun etiketini okunur kılmak içindir.
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    for esik, ek in ((1e9, " Mr"), (1e6, " M"), (1e3, " B")):
        if abs(n) >= esik:
            return f"{n / esik:.1f}".replace(".", ",") + ek
    return f"{n:.0f}"


def yil_grafigi(baslik, satirlar, deger_alani, *, not_metni=None, birim="₺"):
    """
    Yıl serisinden `bar_chart` bloğu üretir (mobil `MiniBarChart` veri şekli).

    ⚠️ `satirlar` KRONOLOJİK (eskiden yeniye) verilmelidir — çağıran taraf sıralamayı
    kendi kaynağına göre düzeltmelidir: `benchmark._yillara_gore` AZALAN,
    `market.grup_detayi.yillara_gore` ARTAN üretir.
    """
    veri = []
    for r in satirlar or []:
        ham = r.get(deger_alani)
        if ham in (None, ""):
            continue
        try:
            deger = float(ham)
        except (TypeError, ValueError):
            continue
        yil = str(r.get("yil") or "")
        adet = r.get("adet") or r.get("sozlesme_sayisi")
        veri.append({
            "key": yil,
            "label": yil,
            "value": deger,
            "valueText": kisa_para(deger) + (f" {birim}" if birim else ""),
            # Örneklemi çok küçük yıl soluk çizilir — grafik yanlış kesinlik vermesin.
            "dim": bool(adet is not None and adet < 3),
        })
    if len(veri) < 2:
        return None  # tek sütunluk "grafik" bilgi vermez, gürültü yapar
    blok = {"type": "bar_chart", "baslik": baslik, "veri": veri}
    if not_metni:
        blok["not"] = not_metni
    return blok
