/*
 * İhaleTakip admin panosu — grafikler (Chart.js 4).
 *
 * ⚠️ Renkler SABİT HEX olarak yazılamaz: jazzmin temayı <html data-bs-theme>
 *    üzerinden çalışma anında değiştirir (default_theme_mode="auto"). Palet her
 *    çizimde getComputedStyle ile CSS değişkenlerinden okunur ve tema değişince
 *    grafikler yeniden çizilir (MutationObserver).
 * ⚠️ Veri <script type="application/json" id="dashboard-data"> düğümünden okunur;
 *    düğüm yoksa (metrik hatası) dosya sessizce çıkar.
 */
(function () {
  "use strict";

  var dugum = document.getElementById("dashboard-data");
  if (!dugum || typeof Chart === "undefined") return;

  var veri;
  try {
    veri = JSON.parse(dugum.textContent);
  } catch (e) {
    return;
  }

  var grafikler = [];
  var sayiBicim = new Intl.NumberFormat("tr-TR");

  function css(ad, yedek) {
    var d = getComputedStyle(document.documentElement).getPropertyValue(ad).trim();
    return d || yedek;
  }

  function palet() {
    var marka = css("--it-blue", "#0074cb");
    return {
      marka: marka,
      koyu: css("--it-accent-2", "#003ea1"),
      metin: css("--bs-body-color", "#212529"),
      izgara: css("--bs-border-color", "rgba(128,128,128,.25)"),
      // Kategorik seriler: marka renginden türeyen, birbirinden ayırt edilebilir set.
      seri: [marka, "#00b894", "#f0932b", "#8e44ad", "#e17055", "#0984e3"]
    };
  }

  function alan(ctx, renk) {
    // Çizgi altı yumuşak dolgu — canvas yüksekliği CSS'ten geldiği için burada
    // sabit piksel kullanmak yerine grafiğin kendi alanına göre gradyan kurulur.
    var g = ctx.createLinearGradient(0, 0, 0, 260);
    g.addColorStop(0, renk + "55");
    g.addColorStop(1, renk + "00");
    return g;
  }

  function ortakSecenekler(p) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function (c) {
              return " " + c.dataset.label + ": " + sayiBicim.format(c.parsed.y ?? c.parsed);
            }
          }
        }
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 12 } },
        y: {
          beginAtZero: true,
          grid: { color: p.izgara },
          // maxTicksLimit olmadan dar ekranda eksen 10+ etiketle doluyor
          ticks: { precision: 0, maxTicksLimit: 6, callback: function (v) { return sayiBicim.format(v); } }
        }
      }
    };
  }

  function yap(id, yapilandirma) {
    var el = document.getElementById(id);
    if (!el) return;
    grafikler.push(new Chart(el, yapilandirma));
  }

  function ciz() {
    grafikler.forEach(function (g) { g.destroy(); });
    grafikler = [];

    var p = palet();
    Chart.defaults.color = p.metin;
    Chart.defaults.borderColor = p.izgara;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

    var ortak = ortakSecenekler(p);

    // 1) 30 günlük yeni kayıt
    if (veri.kayit_30g) {
      yap("chart-kayit-30g", {
        type: "line",
        data: {
          labels: veri.kayit_30g.etiketler,
          datasets: [{
            label: "Yeni kayıt",
            data: veri.kayit_30g.degerler,
            borderColor: p.marka,
            backgroundColor: function (c) { return alan(c.chart.ctx, p.marka); },
            fill: true, tension: 0.32, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2
          }]
        },
        options: ortak
      });
    }

    // 2) 12 aylık büyüme (çubuk + kümülatif ikinci eksen)
    if (veri.kayit_12ay) {
      var ortakKopya = JSON.parse(JSON.stringify({
        responsive: true, maintainAspectRatio: false
      }));
      yap("chart-kayit-12ay", {
        type: "bar",
        data: {
          labels: veri.kayit_12ay.etiketler,
          datasets: [
            {
              label: "Aylık yeni kayıt", data: veri.kayit_12ay.degerler,
              backgroundColor: p.marka, borderRadius: 4, order: 2
            },
            {
              label: "Toplam kullanıcı", data: veri.kayit_12ay.kumulatif,
              type: "line", borderColor: p.koyu, backgroundColor: p.koyu,
              yAxisID: "y2", tension: 0.3, pointRadius: 0, borderWidth: 2, order: 1
            }
          ]
        },
        options: Object.assign({}, ortakKopya, {
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: true, position: "bottom", labels: { boxWidth: 12, usePointStyle: true } },
            tooltip: ortak.plugins.tooltip
          },
          scales: {
            x: { grid: { display: false } },
            y: { beginAtZero: true, grid: { color: p.izgara }, ticks: { precision: 0, maxTicksLimit: 6 } },
            y2: { position: "right", beginAtZero: true, grid: { display: false }, ticks: { precision: 0, maxTicksLimit: 6 } }
          }
        })
      });
    }

    // 3) 30 günlük etkileşim (çok serili)
    if (veri.etkilesim_30g) {
      yap("chart-etkilesim", {
        type: "line",
        data: {
          labels: veri.etkilesim_30g.etiketler,
          datasets: veri.etkilesim_30g.seriler.map(function (s, i) {
            return {
              label: s.ad, data: s.degerler,
              borderColor: p.seri[i % p.seri.length],
              backgroundColor: p.seri[i % p.seri.length],
              tension: 0.32, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2
            };
          })
        },
        options: Object.assign({}, ortak, {
          plugins: {
            legend: { display: true, position: "bottom", labels: { boxWidth: 12, usePointStyle: true } },
            tooltip: ortak.plugins.tooltip
          }
        })
      });
    }

    // 4) Donut'lar
    function donut(id, kayitlar) {
      if (!kayitlar || !kayitlar.length) return;
      yap(id, {
        type: "doughnut",
        data: {
          labels: kayitlar.map(function (k) { return k.ad; }),
          datasets: [{
            data: kayitlar.map(function (k) { return k.n; }),
            backgroundColor: kayitlar.map(function (_, i) { return p.seri[i % p.seri.length]; }),
            borderWidth: 0
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false, cutout: "62%",
          plugins: {
            legend: { position: "bottom", labels: { boxWidth: 12, usePointStyle: true, padding: 12 } },
            tooltip: {
              callbacks: {
                label: function (c) { return " " + c.label + ": " + sayiBicim.format(c.parsed); }
              }
            }
          }
        }
      });
    }
    donut("chart-abonelik", veri.abonelik);
    donut("chart-saglayici", veri.saglayici);

    // 5) Bildirim türleri (yatay çubuk)
    if (veri.bildirim_turu && veri.bildirim_turu.length) {
      yap("chart-bildirim", {
        type: "bar",
        data: {
          labels: veri.bildirim_turu.map(function (t) { return t.ad; }),
          datasets: [{
            label: "Bildirim",
            data: veri.bildirim_turu.map(function (t) { return t.n; }),
            backgroundColor: veri.bildirim_turu.map(function (_, i) { return p.seri[i % p.seri.length]; }),
            borderRadius: 4
          }]
        },
        options: {
          indexAxis: "y",
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: function (c) { return " " + sayiBicim.format(c.parsed.x); } } }
          },
          scales: {
            x: { beginAtZero: true, grid: { color: p.izgara }, ticks: { precision: 0, maxTicksLimit: 6 } },
            y: { grid: { display: false } }
          }
        }
      });
    }
  }

  ciz();

  // Tema değişimini izle (jazzmin/js/main.js data-bs-theme'i canlı değiştirir).
  new MutationObserver(ciz).observe(document.documentElement, {
    attributes: true, attributeFilter: ["data-bs-theme"]
  });
})();
