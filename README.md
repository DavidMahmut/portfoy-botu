# 3.000 $ Portföy Defteri

Sanal bir **ABD kredili hesabında long/short işlem yapan**, kurallara dayalı bir kâğıt üzerinde işlem (paper trading) botu. Gerçek para, aracı kurum ya da yapay zekâ kullanmaz; GitHub Actions'ta ücretsiz çalışır. 8 Ekim 2026'da 300 $ ile başladı; 12 Ekim'de sermaye 2.000 $'a, ardından FINRA'nın 2.000 $ asgari özsermaye kuralına takılmamak için 3.000 $'a çıkarıldı (hesap %33 değer kaybedene kadar short ve kredi yetkisi korunur).

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

## Strateji (özet)

1. **Rejim (her gün):** Güçlü yükseliş → Reg T alım gücünün %55'i long hisse, %20'si 3x endeks ETF'i (TQQQ/UPRO/SOXL), %25'i short. Kararsız → %60 long, %40 short. Düşüş → %30 long, %25 −3x ters ETF (SQQQ/SPXU/SOXS), %45 short. VIX ≥ 35 → alım gücünün %60'ı.
2. **Long:** Momentum puanı (0,2 × 1 ay + 0,5 × 3 ay + 0,3 × 6 ay) en yüksek 4 uygun hisse/ETF; ilk 10'dan düşen satılır.
3. **Short:** 10 $ üstü, 50 günlük ortalamanın altında, 1 ve 3 aylık getirisi negatif hisselerden en zayıf 3'ü; en zayıf 10'dan çıkan kapatılır.
4. **Zarar-kes (bekleyen emir):** Long maliyet × 0,92 / en yüksek × 0,88 (3x ETF: × 0,85 / × 0,80); short maliyet × 1,08 / en düşük × 1,12.
5. **Kâr alma:** ±%20'de yarısı. **Şok:** ters yönde %7 ve 50 günlük ortalamayı kırarsa kapatılır.
6. **Frenler:** SPY −%3 → long maruziyet ≤ 1x (−%5'te 0,5x); SPY +%3 → short maruziyet ≤ 0,5x.

> Basitleştirmeler: ödünç bulma (locate) her zaman başarılı sayılır, temettüler hak kesim gününde nakde geçer, emirler 5 dakikalık mum fiyatından %0,05 kaymayla gerçekleşir.

## Kullanım

- **Sermaye eklemek:** `deposits.json` dosyasına yeni bir satır ekleyin (benzersiz `id`, `date`, `amount`); bot bir sonraki çalışmada işler. Özsermaye 2.000 $'a çok yakınsa küçük bir zarar bile short ve kredi yetkisini kapatır.
- **Elle çalıştırma:** Actions → "Portföy botu" → *Run workflow*.
- **Durdurma:** Actions → "Portföy botu" → *⋯* → *Disable workflow*.
- **Test:** `python test_engine.py` (internet gerektirmez, sahte veriyle senaryoları dener).

> Bu bir simülasyondur; yatırım veya vergi tavsiyesi değildir.
