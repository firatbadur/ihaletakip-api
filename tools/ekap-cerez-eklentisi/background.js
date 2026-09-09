// EKAP doğrulama çerezini periyodik olarak sunucuya iletir.
//
// ⚠️ Bu eklenti CAPTCHA ÇÖZMEZ, tıklama TAKLİT ETMEZ. Doğrulamayı kullanıcının
// kendisi, kendi tarayıcısında geçer; EKAP arayüzü açık sekmede oturumu kendi
// kendine tazeler (scheduleRefresh). Buradaki iş yalnızca tazelenen çerezi
// toplayıcıya taşımaktır.
//
// ⚠️ Gönderim SADECE çerez DEĞİŞTİĞİNDE yapılır: her turda göndermek sunucuya
// ve loglara gereksiz yük bindirirdi.

const ALARM = "ekap-cerez";
const PERIYOT_DK = 2;      // doğrulama ~8 dk yaşıyor → 2 dk güvenli aralık
const EKAP = "ekapv2.kik.gov.tr";

async function ayarlar() {
  const { apiUrl, token } = await chrome.storage.local.get(["apiUrl", "token"]);
  return { apiUrl, token };
}

async function cerezMetni() {
  const cookies = await chrome.cookies.getAll({ domain: EKAP });
  // Analitik çerezleri gönderme: doğrulamayla ilgisi yok ve kullanıcının izini taşır.
  const atilacak = ["_ga", "_gid", "_gcl", "_hj", "_fb", "_uet", "_clck", "_clsk"];
  return cookies
    .filter(c => !atilacak.some(a => c.name.startsWith(a)))
    .map(c => `${c.name}=${c.value}`)
    .join("; ");
}

async function gonder(zorla = false) {
  const { apiUrl, token } = await ayarlar();
  if (!apiUrl || !token) {
    const eksik = [!apiUrl && "sunucu adresi", !token && "paylaşılan sır"]
      .filter(Boolean).join(" ve ");
    return { ok: false, mesaj: `Ayar eksik: ${eksik}` };
  }

  const cookie = await cerezMetni();
  if (!cookie.includes("ekap.human-verification")) {
    return { ok: false, mesaj: "Doğrulama çerezi yok — EKAP sekmesinde doğrulama yapın" };
  }
  const { sonGonderilen } = await chrome.storage.local.get("sonGonderilen");
  if (!zorla && cookie === sonGonderilen) return { ok: true, mesaj: "Değişmedi" };

  try {
    const r = await fetch(`${apiUrl.replace(/\/+$/, "")}/api/v1/ekap/verification-cookie/`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Ekap-Token": token },
      // ⚠️ Çerez GÖNDERME: kimlik paylaşılan sırla kuruluyor. Oturum çerezi
      // giderse sunucuda CSRF kaynak kontrolü tetikleniyor ve istek reddediliyor.
      credentials: "omit",
      body: JSON.stringify({ cookie }),
    });
    const ok = r.ok;
    // ⚠️ Sunucunun mesajını ve gönderilen sırrın uzunluğunu geri ver: 403'te
    // "değer mi yanlış, uç mu kapalı" sorusunu tahminle değil ölçüyle ayırmak
    // için. Sırrın kendisi ASLA yazılmaz, yalnızca uzunluğu.
    let govde = "";
    try { govde = (await r.json())?.message || ""; } catch (e) { govde = ""; }
    await chrome.storage.local.set({
      sonGonderilen: ok ? cookie : sonGonderilen,
      sonDurum: { ok, kod: r.status, an: new Date().toISOString(), govde },
    });
    return {
      ok,
      mesaj: ok ? "Gönderildi"
                : `Sunucu ${r.status} — "${govde}" (gönderilen sır: ${token.length} karakter)`,
    };
  } catch (e) {
    await chrome.storage.local.set({
      sonDurum: { ok: false, kod: 0, an: new Date().toISOString(), hata: String(e) },
    });
    return { ok: false, mesaj: String(e) };
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: PERIYOT_DK });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: PERIYOT_DK });
});
chrome.alarms.onAlarm.addListener(a => { if (a.name === ALARM) gonder(); });
chrome.runtime.onMessage.addListener((msg, _s, cevapla) => {
  if (msg?.tip === "gonder") { gonder(true).then(cevapla); return true; }
});
