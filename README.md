# 3.000 $ Portföy Defteri

Sanal bir **ABD kredili hesabında iki yönlü (long ya da short) işlem yapan**, kurallara dayalı bir kâğıt üzerinde işlem (paper trading) botu. Gerçek para, aracı kurum ya da yapay zekâ kullanmaz; GitHub Actions'ta ücretsiz çalışır. 8 Ekim 2026'da 300 $ ile başladı; 12 Ekim'de sermaye 2.000 $'a, ardından FINRA'nın 2.000 $ asgari özsermaye kuralına takılmamak için 3.000 $'a çıkarıldı (hesap %33 değer kaybedene kadar short ve kredi yetkisi korunur).

**Amaç:** yalnızca gerçekte yapılabilecek işlemleri yapmak ve bu sırada devreye giren yasal kuralları, maliyetleri ve vergi sonuçlarını görünür kılarak öğrenmek. Hesap, Interactive Brokers'ta (IBKR Pro, Tiered) açılmış bir kredili hesap gibi modellenir.

- **Takip sayfası:** GitHub Pages (`index.html`): net değer, kaldıraç, Reg T alım gücü, bakım teminatı, maliyetler, pozisyonlar, her işlemin gerekçesi, mevzuat günlüğü, vergi görünümü, yasal kaynaklar, günlük long ve short sıralamaları.
- **Veri:** Yahoo Finance (yfinance kütüphanesi), 5 dakikalık mumlar ve temettü geçmişi.
- **Zamanlama:** Piyasa açılınca başlayan bir GitHub Actions döngüsü kapanışa kadar 5 dakikada bir çalışır (`.github/workflows/bot.yml`); cron-job.org ve yedek zamanlayıcılar döngüyü başlatır.
- **Kayıt:** Tüm durum `ledger.json` dosyasında (`compliance`: mevzuat günlüğü, `tax`: yıllık vergi görünümü). Sermaye eklemeleri `deposits.json` dosyasından okunur.

## Günlük 3+3 (v8, 27 Kasım 2026'dan itibaren)

Hesap sahibinin kuralı (sanal para): x10 hesabı arşivlendi (`archive/`), yeni 3.000 $ hesap açıldı.

1. **Sabah araştırması (kurallarla, yapay zekâ yok):** evren, piyasa değeri ≥ 2 milyar $, günde ≥ 50 milyon $ işlem gören ve fiyatı ≥ 10 $ olan ABD hisseleri (~1.300). 08:30–09:29 arasında yarım saatlik izleme işi seans öncesi fiyatlarla ön sıralama yapar (`research.py` → `picks.json`); bot açılıştan sonraki ilk çalışmada (09:31'den itibaren) gerçek açılış fiyatlarıyla son sıralamayı yapar (`daytrade.py`).
2. **Seçim:** long = önceki kapanışa göre en çok aşağı boşlukla açılan ama 200 günlük ortalamasının üstündeki 3 hisse; short = en çok yukarı boşlukla açılan 3 hisse. Satın alma haberi olanlar elenir, yaptırım listesindekilere girilmez.
3. **Giriş:** açılışta toplam 3x kaldıraç (her pozisyon hesabın yarısı).
4. **Çıkış:** pozisyon kendi parasına göre %15 kâra ulaşınca (hisse %5 lehte) kapanır; olmazsa 15:55'te (erken kapanış günlerinde 12:55). Zarar-kes yok; geceye pozisyon kalmaz.

**Sıralama neden böyle (`daytrade_study.py`):** 1.320 büyük hissede Ekim 2024–Kasım 2026 arası 520 işlem gününde 6 long ve 6 short sıralaması denendi. Boşluğun yönünde girmek kaybettirdi (yukarı açılanı almak pozisyon başına günde −%0,35, aşağı açılanı açığa satmak −%0,44); tersine girmek kazandırdı (aşağı açılan güçlü hisseyi almak +%0,19, yukarı açılanı açığa satmak +%0,27). Bu ikili 3x kaldıraçla hesaba günde ortalama +%0,69 kattı (günlerin %59'u artı, iki yarı da artıda), ama en kötü düşüş −%57, en kötü gün −%23. 36 ikili denendiği için en iyisinin bir kısmı şanstır.

Önceki testler (`intraday_study.py`, `cohort_study.py`): hareketli hisseleri 10:30'da alıp aynı gün kapatmanın masraflardan sonra kazancı yoktu; günler boyu tutmak daha iyiydi. Bu yüzden günlük kural açılışa ve boşluğun tersine dayanıyor.

Günlük ve haftalık değerlendirme (`review.py` → `review.json`): her gün hesabın değişimi, kural bazında kâr/zarar, işlemler, sıralamada kalan adayların sonra ne yaptığı; her hafta toplamı.

## x10 simülasyonu (v7, 16 Kasım 2026'dan itibaren)

v6 hesabı 2.851,39 $ ile (−%4,95; aynı dönemde SPY +%0,39) kapatılıp arşivlendi (`archive/`) ve yerine yeni bir 3.000 $ hesap açıldı. Amaç artık "gerçek bir hesabı taklit etmek" değil, **yüksek kaldıracın ve haber odaklı işlemlerin nasıl davrandığını izleyip öğrenmek**. Gerçek ABD aracı kurumları küçük bir hesaba hissede 10x kaldıraç vermez; bu yüzden Reg T ve FINRA 4210 teminat kuralları bu hesapta uygulanmaz. Piyasa kuralları (SEC Rule 201), ABD yaptırım listesi ve gerçek masraflar (komisyon, SEC/FINRA ücretleri, kredi faizi, ödünç ücreti, fiyat kayması) uygulanır.

- **Hesap modeli:** her pozisyon için %10 teminat, toplam maruziyet en fazla hesabın 10 katı. Hesap değeri açık maruziyetin %5'inin altına inerse zorunlu tasfiye çalışır. Hesap 300 $'ın altına inerse **batmış** sayılır: her şey kapatılır, hesap arşivlenir, ertesi işlem günü yeni 3.000 $ ile yeniden başlar; batışlar sayfada nedenleriyle listelenir.
- **Endeks kolu:** S&P 500 trendinin yönünde QQQ (long'da krediyle, short'ta açığa satış). Büyüklük = %50 ÷ QQQ'nun 20 günlük oynaklığı; long'da en fazla 10x, short'ta en fazla 3x. `lev_study.py` (1999–2026): %25 hedefle yıllık %13,7 ve en kötü düşüş −%46; **%50 hedefle %16,1 ve −%81**; %100 hedefle %5,6 ve −%99; yön uygun oldukça hep 10x tutmak hesabı 12 kez, QQQ'yu sürekli 10x tutmak 39 kez batırdı. Yani %50, büyümenin düşmeye başlamadığı en yüksek risk düzeyi; 10x tavanının kendisi kâr eklemiyor (kural 9x üstüne günlerin %0,1'inde çıkıyor).
- **Fırsat taraması:** günde dört kez (10:30, 12:00, 13:30, 15:00 ET) Nasdaq, NYSE ve NYSE American'daki bütün hisselerden o gün en az %4 hareket edenlere bakılır (Yahoo tarayıcısı; fiyat ≥ 5 $, günde ≥ 20 milyon $ işlem, piyasa değeri ≥ 300 milyon $). En büyük hareketlerin başlıkları `catalyst.py` ile **kurallarla** sınıflandırılır (dolandırıcılık iddiası, soruşturma, açığa satış raporu, beklenti değişikliği, FDA kararı, satın alma, söylenti, analist notu...). Yapay zekâ kullanılmaz; anahtar kelime kuralları yanlış okuyabilir.
  - **Haber devamı (test edildi):** önceki kapanışın en az %8 üstünde açılan ve ilk saatte en az %0,5 daha yükselen, 2 milyar $+ ve günde 50 milyon $+ işlem gören hisse alınır; zarar-kes ilk saatin en düşüğünün altı, çıkış 2 işlem günü sonra. `event_study.py` (`EVENT_WIDE=1`, 1.966 hisse, Aralık 2023–Kasım 2026): 1.403 işlem, işlem başına +%0,97, kazanç/kayıp oranı 1,36, dönemin iki yarısı da artıda. Büyüklük (zarar-keste hesabın %3'ü, tek işlem en fazla 1x, 6 işlem) aynı testin büyüklük tablosundan seçildi: daha fazlası büyümeyi neredeyse artırmadan düşüşü derinleştiriyor.
  - **Eski haber şoku kuralı kaldırıldı:** ilk saatte boşluğun tersine dönen hisseye girmek aynı geniş testte işlem başına −%0,17 getirdi (v6'da canlıda da çoğu işlem zarar-kesle kapandı).
  - **Haber takibi (deneysel):** sert bir şirket haberiyle en az %5 hareket eden ve günün aralığının haber yönündeki ucunda kalan hissede haber yönünde işlem (olumsuzda short). Geçmiş başlık arşivi olmadığı için sınanamadı; bu yüzden küçük tutulur (zarar-keste hesabın %1'i, en fazla hesabın yarısı, 4 işlem) ve sonucu sayfadaki kural karnesinde ayrı izlenir.
  - **Sert yükseliş short'u:** kural aynı, ama artık 300 milyon $'dan küçük şirketlere girilmiyor (v6'da 50 milyon $'lık OMH short'u bir günde zarar-kese gitti; bu büyüklükte ödünç hisse gerçekte çoğu zaman bulunmaz). `spike_study.py`: 300 milyon $+ şirketlerde 399 işlem, işlem başına +%4,9.
- **Sınırlar:** fırsat işlemlerinin toplamı en fazla hesabın 6 katı, hepsi birlikte en fazla 10 katı; satın alma haberleri, söylentiler, zayıf haberler ve habersiz hareketlerde işlem açılmaz.
- **Geçmiş testlerin sınırı:** bugünkü şirket listesiyle yapıldı (borsadan çıkanlar yok), şirket büyüklüğü bugünkü değerle süzüldü, en iyi görünen ayar geçmişe uydurulmuş olabilir. Kâr garantisi yoktur.

### Bu hafta takvimi ve piyasa göstergeleri

`agenda.py` (haber işiyle birlikte yarım saatte bir, ayrıca pazar akşamı) `agenda.json` dosyasını yazar; sayfanın en üstünde gösterilir: S&P 500, Nasdaq 100, Dow, Russell 2000, VIX, 10 yıllık faiz, dolar endeksi, petrol (WTI ve Brent), doğal gaz, altın, bakır, Bitcoin, euro/dolar ve dolar/TL'nin güncel fiyatı ile günlük ve haftalık değişimi; haftanın ABD ekonomik verileri (enflasyon, istihdam, Fed kararı ve tutanakları, perakende satışlar... önceki, beklenti ve açıklanan değerle), büyük şirket bilançoları, halka arzlar, tatiller ve opsiyon vadeleri. Kaynak: Yahoo Finance takvimleri (yfinance).

## v6'ya kadar uygulanan gerçek hesap kuralları

Aşağıdaki tablo ve v6 bölümleri önceki hesapları anlatır. x10 simülasyonunda Reg T ve FINRA teminat kuralları uygulanmaz; SEC Rule 201, ücretler, faiz, ödünç ve vergi görünümü aynen geçerlidir.


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
- Aynı kural günde 50 milyon $+ işlem gören hisselerde (botun kullandığı, `live` anahtarı): 480 işlem, ortalama %+6,7, kazanma %63, kâr faktörü 1,58, t = 4,1; iki yarıda %+4,5 / %+8,5 (2019 ve 2022 eksi yıllar). Ödünç ücreti yıllık %100 olsa %+4,4, %200 olsa %+1,1. Kol simülasyonu (işlem başına kolun %5'i, en fazla 6 açık, günlük piyasa değerlemeli): yıllık %+12,9, en kötü düşüş %−15. En kötü tek işlem %−78.
- Zarar-kessiz short ortalamada daha kârlı görünür ama tek işlemde %−250'ye kadar kayıp ve kolda %−40'ı aşan düşüş verdi; kullanılmıyor. %20–30 arası sıçramalarda kenar yok; sert düşene (Rule 201 kalktıktan sonra) short zayıf ve ödünç ücretine çok duyarlı; sert düşeni almak zarar ettirdi.
- Uyarılar: küçük hisselerde ödünç her zaman bulunamaz ve ücret yıllık %100'ü aşabilir; batan şirketler veride olmadığı için sonuç short aleyhine biraz eksik; geçmiş sonuç geleceği garanti etmez.

### Nasdaq düşüş takibi (29 Ekim 2026'dan itibaren)

Sayfanın sağ sütununun üstündeki tek satırlık kutu, Nasdaq 100 fonu QQQ'nun tüm zamanların zirvesinden ne kadar aşağıda olduğunu ve −%20 eşiğinin fiyatını gösterir (−%10 düzeltme, −%20 ayı piyasası). Geçmişteki düşüşlerin listesi `dip_results.json`'da durur. `dip_study.py` (`.github/workflows/dip.yml`, sonuç `dip_results.json`) 1999–2026 verisiyle şunu test etti:

- **Yalnızca %20 düşüşleri takip edip almak** (zirveden %20 düşünce QQQ al, yeni zirvede sat): yıllık %5,5, en kötü düşüş %−80 (QQQ al-tut: %10,7 ve %−83). Zamanın üçte birinden fazlasında nakitte kaldığı için yükselişleri kaçırır. 3x fonla yapılsaydı 2000–2002 çöküşünde hesap sıfırlanırdı. Kullanılmıyor.
- **v5'e eklenen kural:** Nasdaq zirvesinden %20 veya daha fazla düşmüşse endeks kolu short açmaz, yeni zirveye kadar nakitte bekler. Endeks kolunun yıllık getirisi 1999–2026'da %14,7'den %15,4'e, 2018–2026'da %17,0'dan %20,2'ye çıktı; en kötü düşüş azaldı.

**Endeks kolu fren testi (5 Kasım 2026, `dip_study.py`):** QQQ zirvesinden %10+ aşağıda ve 50 günlük ortalamasının altındayken maruziyeti 0,5x'e ya da sıfıra indirmek, QQQ 50 günlüğün altındayken 0,5x, 5 günde %5+ düşüşte 0,5x ve hedef oynaklığı %25 yerine %20 / %15 yapmak test edildi. Hiçbiri getiriyi ve en kötü düşüşü birlikte iyileştirmedi: düzeltme freni 1999–2026'da yıllık getiriyi %15,0'dan %11,7'ye, 2010–2026'da %22,0'dan %17,8'e indirdi (dipte satıp toparlanmayı kaçırıyor). %20 hedef oynaklık yalnızca bir risk ayarı: yılda yaklaşık 2–3 puan daha az getiri karşılığında en kötü düşüş 3–5 puan daha küçük. Kural değiştirilmedi.

### Önceki stratejiler

- **v4 (15–23 Ekim 2026):** SPY trend filtresi, %50 QQQ + %50 momentum sepeti, kaldıraçsız. 15–23 Ekim: %−1,0 (SPY %−1,1).
- **v3 (12–15 Ekim 2026):** kaldıraçlı (2,3x) long/short, dar zarar-kes, günlük işlem. %−9,5 (SPY %−1,1). Geriye dönük testte yıllık yaklaşık %−4, en kötü düşüş %−61.

## Nasdaq hareketliler ve %20 bildirimi

`movers.py` (`.github/workflows/movers.yml`) piyasa açıkken saatte bir (New York saatiyle xx:40'ta) ve kapanıştan sonra (16:10) Nasdaq, NYSE ve NYSE American'da işlem gören hisseleri tarar (9 Kasım 2026'ya kadar yalnızca Nasdaq; Nasdaq Trader sembol listeleri) (fiyatı 3 $, günlük işlem hacmi 2 milyon $ altındakiler elenir). Sayfadaki "Nasdaq hareketliler" bölümünde günün ve haftanın en çok düşen ve yükselen 25 hissesi listelenir; semboller Robinhood sayfasına bağlıdır. Tablo ve filtreler üç borsayı da kapsar ("Hisse türü" filtresinde borsa seçilebilir); %20 e-postası, "düşüp dengelenen hisse" uyarısı ve botun sert yükseliş short'u ise test edildikleri Nasdaq hisseleriyle sınırlı kalır. Kapanıştan sonra günde bir kez, o gün %20 veya daha fazla hareket eden Nasdaq hisselerinden yalnızca düşük riskli olanlar GitHub'da bir bildirimle (issue) e-postaya gelir; hareketin şiddetine göre üç seviyede (Seviye 1: %20–30, Seviye 2: %30–50, Seviye 3: %50+). Düşük risk şartları (`movers.py` → `LOW_RISK`): önceki kapanış ≥ 10 $, piyasa değeri ≥ 2 milyar $, günde ortalama ≥ 20 milyon $ işlem, en az 1 yıldır borsada, son 60 günde günlük oynaklık ≤ %5, son 12 ayda ters bölünme yok, satın alma fiyatına sabitlenmemiş, ABD yaptırım listesinde değil. Elenenler e-postada yalnızca adı ve ilk elenme nedeniyle tek satırda geçer; düşük riskli hisse yoksa e-posta gönderilmez. **Düşüp dengelenen hisse bildirimi:** son 10–60 işlem gününde bir günde %20+ düşen, sonra dengelenen (10 gündür yeni dip yok, fiyat 10 ve 20 günlük ortalamanın üstünde, son 10 günün oynaklığı önceki 10 güne göre en az %30 düşük) ve düşük riskli (fiyat ≥ 10 $, piyasa değeri ≥ 2 milyar $, günde ≥ 20 milyon $ işlem, son 12 ayda ters bölünme yok) bir hisse ilk kez bu kurala girdiğinde, kapanıştan sonra "ÖNEMLİ — Düşüp dengelenen hisse" başlıklı ayrı bir GitHub bildirimi (e-posta) gelir; aynı düşüş için bir kez. `rebound_study.py` (sonuç `rebound_results.json`) 2019–2026'da bu kuralı test etti: 20 günde ortalama +%2,0 ama QQQ'ya göre −%0,3, 60 günde QQQ'ya göre −%0,7; düşen hisseyi almanın 40 çeşidinin hiçbiri endeksi güvenilir biçimde geçmedi. Bildirim alım önerisi değildir. **Zamanlama:** GitHub'ın kendi zamanlayıcısı bu taramayı sık sık saatlerce geciktirdi ya da atladı (ör. 6 Kasım'ın kapanış taraması 19:19 ET'de çalıştı). Bu yüzden 7 Kasım 2026'dan beri taramayı botun 5 dakikalık döngüsü (`bot.yml`) başlatır; `movers.yml`'deki zamanlama yalnızca kapanıştan sonra bir yedektir ve aynı gün için ikinci e-posta göndermez.

**15 teknik indikatör (ücretsiz, `indicators.py`):** taranan her hisse için Yahoo Finance verisinden hesaplanır ve tabloda "İndikatörler" görünümünde, hisse adının yanındaki ▸ ile açılan ayrıntıda ve "İndikatör filtreleri" altında kullanılır: EMA 20/50/200, RSI 14, MACD 12/26/9, VWAP (gün içi 5 dakikalık barlardan; yoksa 20 günlük), hacim profili (son 60 günün en yoğun fiyatı, POC), Bollinger 20/2 (%B ve sıkışma), Supertrend 10/3, ATR 14, Stochastic RSI 14/14/3/3, Ichimoku 9/26/52, ADX/DMI 14, OBV (20 günlük ortalamasına göre), Fibonacci (52 haftalık dip–zirve), Anchored VWAP (52 haftalık zirve ve dip gününden) ve klasik pivot noktaları (önceki seansa göre). Bot bu indikatörlerle işlem yapmaz. `indicator_study.py` (`.github/workflows/indicators.yml`, sonuç `indicator_results.json`) 2019–2026'da 3.080 Nasdaq hissesinde 23 klasik sinyali (EMA dizilimi ve kesişimi, RSI 30/70, MACD kesişimleri, VWAP, Bollinger alt/üst bant ve sıkışma kırılımı, Supertrend AL/SAT, Stoch RSI, Ichimoku bulut kırılımı, ADX > 25, OBV, %61,8 Fibonacci, Anchored VWAP, R1 kırılımı, çoklu teyit ve short sinyalleri) ertesi açılışta işleme girip 5/20/60 gün tutarak test etti; her sinyal, aynı hisselerin rastgele günlerde alınmasıyla ve QQQ ile karşılaştırıldı. Hiçbir sinyal QQQ'yu geçmedi; rastgele alıma göre de fark çoğunlukla sıfıra yakın ya da negatifti. Tek sağlam görünen "alt Bollinger bandının altında kapanış, 5 gün tut" işlem başına +%0,15 fark verdi; bu, maliyetlerdeki küçük bir artışla kaybolacak kadar küçük olduğu için bota eklenmedi.

Sayfadaki listeler filtrelenebilir: hisse türü (Nasdaq-100, S&P 500, endeks dışı, Nasdaq Global Select / Global Market / Capital Market), hisse fiyatı, hacim (milyon $/gün), piyasa değeri, trend (fiyat 30 ve 200 günlük ortalamanın üstünde/altında), F/K ve ATR (14 günlük ortalama gerçek aralık, fiyatın %'si). Bunun için `movers.py` taranan her hisse için bu değerleri `movers.json` → `screen` altına yazar; hisse sayısı ve kâr (piyasa değeri ve F/K için) günde bir Yahoo Finance tarayıcısından, endeks üyelikleri haftada bir Wikipedia'dan alınıp `fundamentals.json`'da tutulur. Filtreler tarayıcıda hatırlanır. Son 15 işlem gününün %20'lik hareketleri `spikeHistory` altında saklanır; bot ertesi sabah bu listeden yalnızca "sert yükseliş short'u" kuralına uyanlara girer (yukarıya bakın).

`movers_study.py` (sonuç `movers_study.json`) 2019–2026 arasındaki 15.813 %20'lik günü inceledi: sert yükselenler sonraki 5 günde ortalama %3 geri verdi (medyan %−6), sert düşenler ise ortalama %3 daha düştü (medyan %−6). Sert düşüşten hemen sonra almak 5 günde ortalama %−3,5 getirdi, işlemlerin yalnızca %37'si kârlıydı; batan şirketler veride olmadığı için gerçek sonuç daha kötüdür.

## Stop hesaplayıcı

Sayfadaki "Stop hesaplayıcı" bölümüne bir hisse sembolü yazınca (ya da Hareketliler tablosunda ▸ ayrıntısındaki "Bu hisse için stop hesapla" bağlantısıyla) zarar-kes adayları hesaplanır: seçilen çarpanla ATR stop'u (2 / 2,5 / 3 × ATR), son 20 günün dibinin yarım ATR altı (short'ta zirvesinin üstü), Chandelier (20 gün zirvesi − 3 ATR), Supertrend çizgisi ve destek/direnç seviyelerinin çeyrek ATR ötesi (EMA 50, 200 günlük ortalama, POC, Anchored VWAP, pivot, Bollinger, Ichimoku, 52 hafta). Her adayın girişe uzaklığı ATR cinsinden değerlendirilir (1 ATR'den yakın: gürültüde tetiklenir; 1,5–4 ATR: makul; 4'ten uzak: pozisyon küçük kalır). Önerilen stop, son 20 günün dibi makul uzaklıktaysa odur, değilse ATR stop'udur. Hesap büyüklüğü ve işlem başına risk yüzdesinden adet hesaplanır (risk ÷ stop mesafesi); 2x / 3x kaldıraçlı ürün seçilirse ürüne yatırılacak tutar gösterilir. Veriler `movers.json`'dan (20 günlük dip/zirve `lo20` / `hi20`); taramada olmayan semboller için fiyat ve ATR elle yazılabilir; NYSE ve NYSE American hisseleri de bulunur. Yatırım tavsiyesi değildir.

## Günün dersi

Sayfanın en üstündeki "Günün dersi" kartı her gün bir konu anlatır: önce 15 teknik gösterge (EMA, RSI, MACD, VWAP, hacim profili, Bollinger, Supertrend, ATR, Stochastic RSI, Ichimoku, ADX/DMI, OBV, Fibonacci, Anchored VWAP, pivot noktaları), sonra bir özet ve 55 borsa terimi; liste bitince başa döner (başlangıç 7 Kasım 2026, ziyaretçinin yerel tarihine göre). Gösterge derslerinde temsili bir fiyat serisi üzerinde çizilmiş örnek grafik (üzerine gelince değerler görünür), hesaplama, okuma, tuzaklar, bizim 2019–2026 testimizin sonucu ve son hareketliler taramasından "Bugün piyasada" sayıları vardır. Oklar ve liste ile diğer derslere bakılabilir, "Küçült" o günün dersini kapatır (ertesi gün yeni ders açık gelir). İçerik `lessons.json` dosyasındadır; yatırım tavsiyesi değildir.

## Haberler

`news.py` portföydeki ve izlenen hisseler ile piyasa için Yahoo Finance başlıklarını toplar, konuya göre etiketler (Fed ve faiz, politika, bilanço, analist, enerji, yapay zekâ) ve `news.json`'a yazar; `watch.yml` ile 04:00–20:00 ET arasında 30 dakikada bir çalışır. GitHub'ın zamanlayıcısı bu işi de sık sık atladığı için (7–9 Kasım'da günde birkaç kez çalıştı) 9 Kasım 2026'dan beri seans içinde botun döngüsü onu yarım saatte bir başlatır; kapanıştan sonra (16:10) bir kez başlatılan iş de 20:00'ye kadar yarım saatte bir kendini tekrarlar. Seans öncesi için hâlâ GitHub'ın zamanlayıcısına bağlıdır. `brief.json` elle yazılan Türkçe günlük özettir.

**Seeking Alpha başlıkları (sağ sütun):** `news.py` aynı çalışmada Seeking Alpha'nın herkese açık RSS akışlarını okur: piyasa haberleri (`market_currents.xml`), tüm yazılar (`feed.xml`) ve botun elindeki her hisse için hisse akışı. Yalnızca başlık, saat, tür (haber / analiz), ilgili semboller ve bağlantı saklanır (`news.json` → `seekingAlpha`); yazıların tamamı Seeking Alpha'da açılır ve bir kısmı abonelik ister. seekingalpha.com isteği reddederse aynı Seeking Alpha başlıkları Google News'in RSS aramasından alınır; ikisi de çalışmazsa son alınan liste "eski" notuyla gösterilir. Sayfada "Piyasa akışı" ve "Elimizdekiler" sekmeleri vardır; botta açık pozisyonu olan semboller turuncu işaretlenir. Bot bu başlıklarla işlem yapmaz.

## Kullanım

- **Sermaye eklemek:** `deposits.json` dosyasına yeni bir satır ekleyin (benzersiz `id`, `date`, `amount`); bot bir sonraki çalışmada işler. Özsermaye 2.000 $'a çok yakınsa küçük bir zarar bile short ve kredi yetkisini kapatır.
- **Elle çalıştırma:** Actions → "Portföy botu" → *Run workflow*.
- **Durdurma:** Actions → "Portföy botu" → *⋯* → *Disable workflow*.
- **Test:** `python test_engine.py` (internet gerektirmez, sahte veriyle senaryoları dener).

> Bu bir simülasyondur; yatırım veya vergi tavsiyesi değildir.
