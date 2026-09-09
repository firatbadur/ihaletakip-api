const $ = id => document.getElementById(id);

chrome.storage.local.get(["apiUrl", "token", "sonDurum"], v => {
  $("apiUrl").value = v.apiUrl || "";
  $("token").value = v.token || "";
  if (v.sonDurum) {
    const d = v.sonDurum;
    $("durum").textContent =
      `${d.ok ? "✅ gönderildi" : "❌ hata " + (d.kod || d.hata || "")} · ` +
      new Date(d.an).toLocaleTimeString("tr-TR");
  }
});

$("kaydet").onclick = () => {
  chrome.storage.local.set(
    { apiUrl: $("apiUrl").value.trim(), token: $("token").value.trim() },
    () => ($("durum").textContent = "Ayarlar kaydedildi.")
  );
};

// ⚠️ "Şimdi gönder" önce ekrandaki alanları KAYDEDER, sonra gönderir.
// Aksi hâlde kullanıcı alanları doldurup Kaydet'e basmadan gönderdiğinde
// "Ayarlar eksik" hatası alıyor ve sebebini göremiyordu.
$("simdi").onclick = () => {
  $("durum").textContent = "Gönderiliyor...";
  chrome.storage.local.set(
    { apiUrl: $("apiUrl").value.trim(), token: $("token").value.trim() },
    () => chrome.runtime.sendMessage({ tip: "gonder" }, r => {
      $("durum").textContent = (r?.ok ? "✅ " : "❌ ") + (r?.mesaj || "yanıt yok");
    })
  );
};
