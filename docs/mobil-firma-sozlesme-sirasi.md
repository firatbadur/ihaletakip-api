# Firma ekranından ihaleye geçiş — gelinen firmanın sözleşmesi önce

**Durum:** backend hazır (2026-10-30). Mobil değişiklik gerekiyor; değişiklik yapılmazsa
uygulama bugünkü gibi çalışmaya devam eder.

## Sorun (kullanıcı bildirimi)

"MEDTRONİC … yeni iş aldı" bildirimi → firma ekranı → ihale (İKN 2026/1167073) →
**Sözleşme** sekmesi. İhale kısımlı, iki sözleşmesi var. İlk kart **DİSAMED**'in
sözleşmesi (bedeli daha yüksek olduğu için listede önde). Medtronic'in kartı ekranın
altında kalıyor. Kullanıcı bunu "firma bu işi almamış" diye okuyor.

Veri doğru: iki sözleşme de bu ihaleye ait. Sorun yalnızca sıralama ve bağlam.

## Backend

`GET /ekap/tenders/<ekap_id>/contracts/?yuklenici_id=<firma id>`

- Bu firmanın sözleşmesi listenin **başına** gelir. Firmanın ortak girişim üyesi olduğu
  sözleşmeler de buna dahil.
- Liste **daraltılmaz**: diğer kısımların yüklenicileri yine gelir, yalnızca sırası değişir.
- Parametre yoksa ya da geçersizse sıra eskisi gibi kalır (sözleşme bedeline göre azalan).
- Yanıtın şekli aynı.

## Mobilde yapılacaklar

1. `ContractorDetail/index.js` içindeki `goTender` fonksiyonu firma kimliğini de geçirsin,
   Sözleşme sekmesi de doğrudan açılsın:
   ```js
   navigation.navigate('TenderDetail', {
     ihaleId: ekapId,
     yukleniciId: contractorId,   // ekrandaki firmanın id'si
     initialTab: 'sozlesme',      // DETAIL_TABS anahtarı
   });
   ```
2. `TenderDetail` bu parametreyi `route.params`'tan okuyup `SozlesmeBilgileriTab`'a
   `yukleniciId` prop'u olarak versin.
3. `getTenderContracts(ekapId, yukleniciId)` fonksiyonu, `yukleniciId` doluysa
   `?yuklenici_id=` parametresini eklesin.
4. (Öneri) Kısımlı ihalede ilk kartın üstünde, `yukleniciId` ile eşleşiyorsa
   "Bu firmanın aldığı kısım" etiketi gösterilsin. Diğer kartlar "Diğer kısımlar"
   başlığı altında kalsın.

Bildirimden doğrudan ihaleye giden bir yol eklenirse (`Notification.contractor_id` +
`tender_ikn`) aynı parametre orada da gönderilmeli.

## İlgili backend düzeltmesi (mobil değişiklik GEREKMEZ)

Aynı ekranda ihale hâlâ "Katılıma Açık" görünüyordu. Sebep: detay ucu durumu
1 Eylül'de saklanmış ham detaydan döndürüyordu. Artık ihale detayı
(`GET /ekap/tenders/<key>/`) şu alanları güncel veriden yazıyor:
- `ihaleDurum`, `ihaleBilgi.ihaleDurum` ve `ihaleBilgi.ihaleDurumAciklama`: liste
  ekranıyla aynı kaynaktan.
- `ilanList`: sonradan gelen sonuç ilanları da ekleniyor.

Sözleşmesi olan bir ihale artık hiçbir ekranda "Katılıma Açık" ya da "Değerlendirmede"
görünmez; durumu en az "Sözleşme İmzalanmış" olur.
