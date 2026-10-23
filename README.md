# 3.000 $ Portföy Defteri

Sanal bir **ABD kredili hesabında iki yönlü (long ya da short) işlem yapan**, kurallara dayalı bir kâğıt üzerinde işlem (paper trading) botu. Gerçek para, aracı kurum ya da yapay zekâ kullanmaz; GitHub Actions'ta ücretsiz çalışır. 8 Ekim 2026'da 300 $ ile başladı; 12 Ekim'de sermaye 2.000 $'a, ardından FINRA'nın 2.000 $ asgari özsermaye kuralına takılmamak için 3.000 $'a çıkarıldı (hesap %33 değer kaybedene kadar short ve kredi yetkisi korunur).

**Amaç:** yalnızca gerçekte yapılabilecek işlemleri yapmak ve bu sırada devreye giren yasal kuralları, maliyetleri ve vergi sonuçlarını görünür kılarak öğrenmek. Hesap, Interactive Brokers'ta (IBKR Pro, Tiered) açılmış bir kredili hesap gibi modellenir.

- **Takip sayfası:** GitHub Pages (`index.html`): net değer, kaldıraç, Reg T alım gücü, bakım teminatı, maliyetler, pozisyonlar, her işlemin gerekçesi, mevzuat günlüğü, vergi görünümü, yasal kaynaklar, günlük long ve short sıralamaları.
- **Veri:** Yahoo Finance (yfinance kütüphanesi), 5 dakikalık mumlar ve temettü geçmişi.
- **Zamanlama:** Piyasa açılınca başlayan bir GitHub Actions döngüsü kapanışa kadar 5 dakikada bir çalışır (`.github/workflows/bot.yml`); cron-job.org ve yedek zamanlayıcılar döngüyü başlatır.
- **Kayıt:** Tüm durum `ledger.json` dosyasında (`compliance`: mevzuat günlüğü, `tax`: yıllık vergi görünümü). Sermaye eklemeleri `deposits.json` dosyasından okunur.

## Uygulanan gerçek kurallar

| Kural | Ne yapar | Kaynak |
|---|---|---|
| FINRA 4210(b)(4) | Açığa satış ve kredili alım için en az 2.000 $ özsermaye. Altında hesap nakit hesabı gibi çalışır. | [FINRA 4210](https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210), [RN 21-24](https://www.finra.org/rules-guidance/notices/21-24) |
| Reg T başlangıç teminatı | Long %50, short %150 (satış geliri + %50), 3x ETF %75. Emir sonrası başlangıç teminatı özsermayeyi aşacaksa emir reddedilir veya küçültülür. | [FINRA 4210](https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210), [RN 09-53](https://www.finra.org/rules-guidance/notices/09-53) |
| FINRA 4210 bakım teminatı | Long %25, short %30 (hisse başı en az 5 $), 3x ETF %75. Altına inilirse en zararlı pozisyondan başlanarak kapatılır. | [FINRA 4210](https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210) |
| Gün içi işlem | Eski PDT kuralı (25.000 $) 4 Haziran 2026'da kaldırıldı; yerine gün içi teminat yeterliliği. | [FINRA](https://www.finra.org/compliance-tools/weekly-archive/04152026) |
| SEC Rule 201 | Hisse önceki kapanışa göre %10 düşünce o gün ve ertesi gün yeni short açılmaz. | [SEC](https://www.sec.gov/news/press/2010/2010-26.htm) |
| Ücretler | IBKR Pro Tiered komisyonu (0,0035 $/hisse, en az 0,35 $, en fazla %1); satışlarda SEC ücreti (milyon $'da 20,60 $) ve FINRA TAF; %0,05 kayma. | [IBKR](https://investors.interactivebrokers.com/en/pricing/commissions-home.php), [SEC](https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2) |
| Faiz ve ödünç | Kredi faizi yıllık %5,38 (IBKR, 100 bin $ altı), ödünç ücreti yıllık %0,5. | [IBKR](https://interactivebrokers.ca/en/index.php?f=47205) |
| Vergi (görünüm) | Elde tutma süresi, kısa/uzun vade, wash sale (30 gün), temettü ve short'ta ödenen temettü, kredi faizi (Form 4952). | [IRS 409](https://www.irs.gov/taxtopics/tc409), [Pub 550](https://www.irs.gov/publications/p550) |

12 Ekim 13:01'deki v2 işlemleri (3x'e yakın kaldıraç) Reg T alım gücünü aştığı için gerçek bir hesapta reddedilirdi; v3'e geçişte iptal edildi, pozisyonlar o anın öncesine döndürüldü (`v3_restore.json`) ve ilk işlemlere IBKR komisyonu geriye dönük eklendi.

## 3 aylık karne (v5: 23 Ekim 2026'dan itibaren 3 ay)

Gerçek paraya geçmeden önce bot üç ay sanal çalışır. Sayfadaki karne her çalışmada güncellenir (`ledger.json` → `scorecard`): zaman ağırlıklı getiri ve SPY farkı, en büyük düşüş, kazanan işlem oranı, kâr faktörü, aylık getiriler ve botun kaç işlem gününde çalıştığı. Şartlar: SPY'ı masraflar düşüldükten sonra geçmek, en büyük düşüşün %20'den az olması, teminat çağrısı olmaması ve botun işlem günlerinin en az %95'inde çalışması.

## Seans dışı izleme

`watch.py` (`.github/workflows/watch.yml`) piyasa öncesi (04:00–09:30 ET) ve sonrası (16:00–20:00 ET) 30 dakikada bir açık pozisyonların seans dışı fiyatını Yahoo Finance'ten alır ve `extended.json`'a yazar; sayfada "Seans dışı izleme" panelinde gösterilir. Yalnızca izlemedir: bot bu saatlerde işlem yapmaz.

## Strateji (v5, 23 Ekim 2026'dan itibaren): iki yönlü, en fazla 3x

1. **Yön (her gün, dünkü kapanışla):** SPY 200 günlük ortalamasının üstündeyse yön yukarı. Altındaysa ve 50 günlük ortalaması da 200 günlüğün altındaysa yön aşağı. Arada kalırsa kararsız (nakit).
2. **Endeks kolu (%50):** hedef maruziyet = %25 ÷ QQQ'nun 20 günlük yıllık oynaklığı, en fazla 3x. Yukarıda QQQ + 3x fon TQQQ, aşağıda −3x ters fon SQQQ. Hepsi nakitle alınır (kredi yok). Hedefle fark 0,3x'ten küçükse işlem yapılmaz.
3. **Momentum sepeti (%50):** SPY 200 günlük ortalamasının üstündeyken 12-1 ay getirisi en yüksek, 200 günlük ortalamasının üstündeki 10 hisse, eşit ağırlık, sektör başına en fazla 3. Ayın ilk işlem günü yenilenir; ilk 20'de kalan tutulur. SPY ortalamanın altına inerse sepet nakde geçer.
4. **Felaket zarar-kesi (sepet):** maliyetin %25 aleyhine; sonra 30 gün o hisseye girilmez. Endeks kolunda fiyat zarar-kesi yok, risk büyüklükle yönetilir.
5. **Sınırlar:** toplam ekonomik maruziyet en fazla 2x (3x × %50 + 1x × %50); hesap düzeyinde 3x, Reg T ve FINRA 4210 her çalışmada kontrol edilir.

**Neden değişti:** v4 (kaldıraçsız, yalnızca long) 15–23 Ekim'de SPY ile başa baş gitti ama düşüşten kazanamıyordu; trend filtresi SPY'a baktığı için Nasdaq ve çipler düşerken tam yatırımda kaldı. İki yönlü, oynaklık hedefli varyantlar `backtest.py` ile 2018–2026 günlük verilerle karşılaştırıldı (`backtest_results.json`): v5 yıllık %20,4, en kötü düşüş %−31 (3.000 $ → 15.315 $); v4 %18,7 ve %−26; SPY %14,2 ve %−34. Yönü QQQ'nun kendi trendinden almak (%−40'a varan düşüş) ve sepette zayıf hisseleri short'lamak (%−45 ile %−58) daha kötü çıktığı için kullanılmadı. Aynı kurallar yalnızca long olsaydı yıllık %21,4 ve %−29,5 olurdu: short tarafı 2022 gibi uzun düşüşlerde korur, hızlı toparlanmalarda maliyet yaratır. Uyarı: hisse listesi bugünün başarılı şirketlerinden oluştuğu için sepet sonuçları iyimserdir; 3x fonlar oynak ve yatay piyasada değer kaybeder.

### Önceki stratejiler

- **v4 (15–23 Ekim 2026):** SPY trend filtresi, %50 QQQ + %50 momentum sepeti, kaldıraçsız. 15–23 Ekim: %−1,0 (SPY %−1,1).
- **v3 (12–15 Ekim 2026):** kaldıraçlı (2,3x) long/short, dar zarar-kes, günlük işlem. %−9,5 (SPY %−1,1). Geriye dönük testte yıllık yaklaşık %−4, en kötü düşüş %−61.

## Haberler

`news.py` portföydeki ve izlenen hisseler ile piyasa için Yahoo Finance başlıklarını toplar, konuya göre etiketler (Fed ve faiz, politika, bilanço, analist, enerji, yapay zekâ) ve `news.json`'a yazar; `watch.yml` ile 04:00–20:00 ET arasında 30 dakikada bir çalışır. `brief.json` elle yazılan Türkçe günlük özettir.

## Kullanım

- **Sermaye eklemek:** `deposits.json` dosyasına yeni bir satır ekleyin (benzersiz `id`, `date`, `amount`); bot bir sonraki çalışmada işler. Özsermaye 2.000 $'a çok yakınsa küçük bir zarar bile short ve kredi yetkisini kapatır.
- **Elle çalıştırma:** Actions → "Portföy botu" → *Run workflow*.
- **Durdurma:** Actions → "Portföy botu" → *⋯* → *Disable workflow*.
- **Test:** `python test_engine.py` (internet gerektirmez, sahte veriyle senaryoları dener).

> Bu bir simülasyondur; yatırım veya vergi tavsiyesi değildir.
