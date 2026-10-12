# 2.000 $ Portföy Defteri

Sanal bir **kredili hesapta long/short işlem yapan**, kurallara dayalı bir kâğıt üzerinde işlem (paper trading) botu. Gerçek para, aracı kurum ya da yapay zekâ kullanmaz; GitHub Actions'ta ücretsiz çalışır. 8 Ekim 2026'da 300 $ ile başladı, 12 Ekim'de sermaye 2.000 $'a tamamlandı.

- **Takip sayfası:** GitHub Pages (`index.html`): net değer, kaldıraç, teminat, maliyetler, pozisyonlar, her işlemin gerekçesi, risk kontrolleri, günlük long ve short sıralamaları.
- **Veri:** Yahoo Finance (yfinance kütüphanesi), 5 dakikalık mumlar.
- **Zamanlama:** Piyasa açılınca başlayan bir GitHub Actions döngüsü kapanışa kadar 5 dakikada bir çalışır (`.github/workflows/bot.yml`); cron-job.org ve yedek zamanlayıcılar döngüyü başlatır.
- **Kayıt:** Tüm durum `ledger.json` dosyasında. Sermaye eklemeleri `deposits.json` dosyasından okunur.

## Kurallar (özet)

1. **Rejim (her gün):** Güçlü yükseliş → en fazla 3x kaldıraç, %75 long / %25 short, long tarafın üçte biri 3x endeks ETF'i (TQQQ/UPRO/SOXL). Kararsız → 2x, %60/%40. Düşüş (SPY 200 günlük ortalamanın altında) → 1,5x, %30/%70, short tarafın üçte biri −3x ters ETF (SQQQ/SPXU/SOXS). VIX ≥ 35 → en fazla 1,5x.
2. **Long:** Momentum puanı (0,2 × 1 ay + 0,5 × 3 ay + 0,3 × 6 ay) en yüksek 4 uygun hisse/ETF; ilk 10'dan düşen satılır.
3. **Short:** 10 $ üstü, 50 günlük ortalamanın altında, 1 ve 3 aylık getirisi negatif hisselerden en zayıf 3'ü; en zayıf 10'dan çıkan kapatılır.
4. **Zarar-kes (bekleyen emir):** Long maliyet × 0,92 / en yüksek × 0,88 (3x ETF: × 0,85 / × 0,80); short maliyet × 1,08 / en düşük × 1,12.
5. **Kâr alma:** ±%20'de yarısı. **Şok:** ters yönde %7 ve 50 günlük ortalamayı kırarsa kapatılır.
6. **Frenler:** SPY −%3 → long maruziyet ≤ 1x (−%5'te 0,5x); SPY +%3 → short maruziyet ≤ 0,5x.
7. **Teminat:** long %25, short %30, 3x ETF %75; açık oluşursa en zararlı pozisyondan başlanarak kapatılır.
8. **Maliyetler:** %0,05 kayma, SEC ücreti (milyon $'da 20,60 $) ve FINRA TAF (hisse başına 0,000195 $), kredi faizi yıllık %10, ödünç ücreti yıllık %0,5.

> Gerçek hayat notu: ABD'de 2.000 $'lık kredili hesapta gece taşınan kaldıraç normalde 2x ile sınırlıdır ve çoğu kurum kesirli hisse açığa satışına izin vermez; 3x burada simülasyondur.

## Kullanım

- **Sermaye eklemek:** `deposits.json` dosyasına yeni bir satır ekleyin (benzersiz `id`, `date`, `amount`); bot bir sonraki çalışmada işler.
- **Elle çalıştırma:** Actions → "Portföy botu" → *Run workflow*.
- **Durdurma:** Actions → "Portföy botu" → *⋯* → *Disable workflow*.
- **Test:** `python test_engine.py` (internet gerektirmez, sahte veriyle senaryoları dener).

> Bu bir simülasyondur; yatırım tavsiyesi değildir.
