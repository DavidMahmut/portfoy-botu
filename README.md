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

## 3 aylık karne (v6: 2 Kasım 2026'dan itibaren 3 ay)

Gerçek paraya geçmeden önce bot üç ay sanal çalışır. Sayfadaki karne her çalışmada güncellenir (`ledger.json` → `scorecard`): zaman ağırlıklı getiri ve SPY farkı, en büyük düşüş, kazanan işlem oranı, kâr faktörü, aylık getiriler ve botun kaç işlem gününde çalıştığı. Şartlar: SPY'ı masraflar düşüldükten sonra geçmek, en büyük düşüşün %20'den az olması, teminat çağrısı olmaması ve botun işlem günlerinin en az %95'inde çalışması.

## Seans dışı izleme

`watch.py` (`.github/workflows/watch.yml`) piyasa öncesi (04:00–09:30 ET) ve sonrası (16:00–20:00 ET) 30 dakikada bir açık pozisyonların seans dışı fiyatını Yahoo Finance'ten alır ve `extended.json`'a yazar; sayfada "Seans dışı izleme" panelinde gösterilir. Yalnızca izlemedir: bot bu saatlerde işlem yapmaz.

## Strateji (v6, 2 Kasım 2026'dan itibaren): yeni 3.000 $ hesap, iki yönlü

Önceki hesap (8 Ekim – 30 Ekim, 3.000 $ yatırıldı, son değer yaklaşık 2.553 $) kapatıldı; özeti `ledger.json` → `previousAccounts`, tam kaydı `archive/` klasöründe.

1. **Endeks kolu (%50):** S&P 500 (SPY) 200 günlük ortalamasının üstündeyse long, altındaysa ve 50 günlük de 200 günlüğün altındaysa short, arada nakit. Büyüklük %25 ÷ QQQ'nun 20 günlük oynaklığı, en fazla 3x; QQQ + 3x fon TQQQ ya da −3x ters fon SQQQ, hepsi nakitle.
2. **Haber şoku kolu (%50):** her gün 10:30'da 166 büyük hisse taranır. Önceki kapanışa göre en az %8 boşlukla açılan ve ilk saatte bu tepkinin tersine dönen hisseye o yönde girilir: sert düşüp toparlanana long, sert yükselip sönene short. Zarar-kes ilk saatin öbür ucu, büyüklük zarar-keste kaybın kolun yaklaşık %1'i olacağı şekilde (en fazla kolun %25'i), aynı anda en fazla 4 işlem, çıkış 2 işlem günü sonra kapanışta. Haberin yönünde devam edenlere girilmez. SEC Rule 201 nedeniyle %10'dan fazla düşmüş hisseye short açılmaz.
3. **Sert yükseliş short'u (haber kolunun ikinci tetiği, Kasım 2026'dan itibaren):** bir Nasdaq hissesi bir günde en az %30 yükselerek kapanırsa ertesi sabah açılışta (09:35 ET) short açılır. Şartlar: önceki gün gerçek fiyatı en az 5 $, son 20 günde günde ortalama en az 50 milyon $ işlem, satın alma haberiyle yatay seyretmiyor, ABD yaptırım listesinde değil, o sabah %10'dan fazla düşmemiş (SEC Rule 201). Büyüklük haber kolunun %5'i, aynı anda en fazla 6 işlem, zarar-kes girişin %50 üstü, çıkış 10. işlem gününün kapanışında. Ödünç ücreti yıllık %30 varsayılır (`borrowRate`), fiyat kayması işlem başına %0,15. Liste `movers.py`'nin kapanıştan sonra yazdığı `movers.json` → `spikeHistory`'den okunur.
4. **Momentum sepeti kapalı** (yalnızca long çalışıyordu).

**Test (`event_study.py`, sonuç `event_results.json`, Aralık 2023 – Ekim 2026, 164 hisse, 5.669 haber günü):** haberin yönünde girmek zarar ettirdi (%3+ boşlukta işlem başına ortalama %−0,23). Yalnızca %8+ şoklarda ilk saatteki dönüşün yönünde girmek işlem başına ortalama %+0,74 getirdi (386 işlem, kazanma oranı %47); 3.000 $'lık kol için yaklaşık 3 yılda %+33, en kötü düşüş %−9; dönemin iki yarısında da kârlı, ama ikinci yarıda işlem başına kazanç azaldı. Kâr garantisi yoktur.

**Sert yükseliş testi (`spike_study.py`, `.github/workflows/spike_study.yml`, sonuç `spike_results.json`):** 2019–2026, bugün Nasdaq'ta işlem gören yaklaşık 3.400 hisse, 46.549 adet %20'lik gün. Fiyat filtresi o gün gerçekten işlem gören fiyatla yapılır (Yahoo geçmişi sonraki ters bölünmelere göre düzeltildiği için aksi halde dünün kuruş hisseleri 5 $ filtresinden geçer ve sonuç olduğundan iyi görünür; ilk denemede bu hata vardı). Maliyet: gidiş-dönüş %0,4, zarar-keste %1 ek kayma, yıllık %30 ödünç ücreti.

- %30+ yükselen, günde 20 milyon $+ işlem gören hisseye ertesi açılışta short, %50 zarar-kes, 10 gün: 866 işlem, ortalama %+4,4, kazanma %60; dönemin iki yarısında da kârlı (%+1,8 / %+6,1). Ödünç ücreti yıllık %100 olsa %+2,1, %200 olsa %−1,1.
- Aynı kural günde 50 milyon $+ işlem gören hisselerde (botun kullandığı): 482 işlem, ortalama %+6,9, kazanma %63, kâr faktörü 1,6; iki yarıda %+4,5 / %+8,8. En kötü tek işlem %−78.
- Zarar-kessiz short ortalamada daha kârlı görünür ama tek işlemde %−250'ye kadar kayıp ve kolda %−40'ı aşan düşüş verdi; kullanılmıyor. %20–30 arası sıçramalarda kenar yok; sert düşene (Rule 201 kalktıktan sonra) short zayıf ve ödünç ücretine çok duyarlı; sert düşeni almak zarar ettirdi.
- Uyarılar: küçük hisselerde ödünç her zaman bulunamaz ve ücret yıllık %100'ü aşabilir; batan şirketler veride olmadığı için sonuç short aleyhine biraz eksik; geçmiş sonuç geleceği garanti etmez.

### Nasdaq düşüş takibi (29 Ekim 2026'dan itibaren)

Sayfanın kenarındaki panel, Nasdaq 100 fonu QQQ'nun tüm zamanların zirvesinden ne kadar aşağıda olduğunu gösterir (−%10 düzeltme, −%20 ayı piyasası). `dip_study.py` (`.github/workflows/dip.yml`, sonuç `dip_results.json`) 1999–2026 verisiyle şunu test etti:

- **Yalnızca %20 düşüşleri takip edip almak** (zirveden %20 düşünce QQQ al, yeni zirvede sat): yıllık %5,5, en kötü düşüş %−80 (QQQ al-tut: %10,7 ve %−83). Zamanın üçte birinden fazlasında nakitte kaldığı için yükselişleri kaçırır. 3x fonla yapılsaydı 2000–2002 çöküşünde hesap sıfırlanırdı. Kullanılmıyor.
- **v5'e eklenen kural:** Nasdaq zirvesinden %20 veya daha fazla düşmüşse endeks kolu short açmaz, yeni zirveye kadar nakitte bekler. Endeks kolunun yıllık getirisi 1999–2026'da %14,7'den %15,4'e, 2018–2026'da %17,0'dan %20,2'ye çıktı; en kötü düşüş azaldı.

### Önceki stratejiler

- **v4 (15–23 Ekim 2026):** SPY trend filtresi, %50 QQQ + %50 momentum sepeti, kaldıraçsız. 15–23 Ekim: %−1,0 (SPY %−1,1).
- **v3 (12–15 Ekim 2026):** kaldıraçlı (2,3x) long/short, dar zarar-kes, günlük işlem. %−9,5 (SPY %−1,1). Geriye dönük testte yıllık yaklaşık %−4, en kötü düşüş %−61.

## Nasdaq hareketliler ve %20 bildirimi

`movers.py` (`.github/workflows/movers.yml`) piyasa açıkken saatte bir ve kapanıştan sonra Nasdaq'ta işlem gören yaklaşık 3.400 hisseyi tarar (fiyatı 3 $, günlük işlem hacmi 2 milyon $ altındakiler elenir). Sayfadaki "Nasdaq hareketliler" bölümünde günün ve haftanın en çok düşen ve yükselen 25 hissesi listelenir; semboller Robinhood sayfasına bağlıdır. Bir hisse gün içinde %20 veya daha fazla hareket ederse GitHub'da bir bildirim (issue) açılır ve depo sahibine e-posta / uygulama bildirimi gider. Son 15 işlem gününün %20'lik hareketleri `spikeHistory` altında saklanır; bot ertesi sabah bu listeden yalnızca "sert yükseliş short'u" kuralına uyanlara girer (yukarıya bakın).

`movers_study.py` (sonuç `movers_study.json`) 2019–2026 arasındaki 15.813 %20'lik günü inceledi: sert yükselenler sonraki 5 günde ortalama %3 geri verdi (medyan %−6), sert düşenler ise ortalama %3 daha düştü (medyan %−6). Sert düşüşten hemen sonra almak 5 günde ortalama %−3,5 getirdi, işlemlerin yalnızca %37'si kârlıydı; batan şirketler veride olmadığı için gerçek sonuç daha kötüdür.

## Haberler

`news.py` portföydeki ve izlenen hisseler ile piyasa için Yahoo Finance başlıklarını toplar, konuya göre etiketler (Fed ve faiz, politika, bilanço, analist, enerji, yapay zekâ) ve `news.json`'a yazar; `watch.yml` ile 04:00–20:00 ET arasında 30 dakikada bir çalışır. `brief.json` elle yazılan Türkçe günlük özettir.

## Kullanım

- **Sermaye eklemek:** `deposits.json` dosyasına yeni bir satır ekleyin (benzersiz `id`, `date`, `amount`); bot bir sonraki çalışmada işler. Özsermaye 2.000 $'a çok yakınsa küçük bir zarar bile short ve kredi yetkisini kapatır.
- **Elle çalıştırma:** Actions → "Portföy botu" → *Run workflow*.
- **Durdurma:** Actions → "Portföy botu" → *⋯* → *Disable workflow*.
- **Test:** `python test_engine.py` (internet gerektirmez, sahte veriyle senaryoları dener).

> Bu bir simülasyondur; yatırım veya vergi tavsiyesi değildir.
