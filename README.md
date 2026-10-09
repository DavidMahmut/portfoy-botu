# 300 $ Portföy Defteri

Sanal 300 $ ile ABD hisse ve ETF'lerinde işlem yapan, kurallara dayalı bir **kâğıt üzerinde işlem (paper trading) botu**. Gerçek para, aracı kurum ya da yapay zekâ kullanmaz; GitHub Actions'ta ücretsiz çalışır.

- **Takip sayfası:** GitHub Pages (`index.html`) — portföy değeri, pozisyonlar, her işlemin gerekçesi, risk kontrolleri, günlük momentum sıralaması.
- **Veri:** Yahoo Finance (yfinance kütüphanesi), 5 dakikalık mumlar.
- **Zamanlama:** Piyasa açılınca başlayan bir GitHub Actions döngüsü kapanışa kadar 5 dakikada bir çalışır (`.github/workflows/bot.yml`). Yedek tetikleyiciler, GitHub bir tetiklemeyi geciktirirse döngüyü yeniden başlatır.
- **Kayıt:** Tüm durum `ledger.json` dosyasında; her çalışma bir commit olarak geçmişte görünür.

## Kurallar (özet)

1. Evren: ~95 likit ABD hissesi ve kaldıraçsız ETF (`universe.py`).
2. Momentum puanı = 0,2 × 1 ay + 0,5 × 3 ay + 0,3 × 6 ay getiri. Fiyat 50 günlük ortalamanın üstünde ve 3 aylık getiri pozitif olmalı.
3. Her gün 10:00'dan (New York) sonraki ilk çalışmada: en iyi 4 varlık hedeflenir; ilk 10'dan düşen satılır.
4. SPY 200 günlük ortalamanın altındaysa en fazla %50 yatırım.
5. Bekleyen zarar-kes: maliyet × 0,92 ve en yüksek × 0,88'in büyüğü; 5 dakikalık mumlarla sırayla kontrol edilir.
6. +%20'de yarısı satılır (bir kez).
7. Gün içi −%7 ve 50 günlük ortalamanın altı → satış.
8. Çöküş freni: SPY −%3 → nakit ≥ %50; −%5 → nakit ≥ %80; o gün alım yok.

## Kullanım

- **Elle çalıştırma:** Actions → "Portföy botu" → *Run workflow* (piyasa kapalıyken denemek için "force" kutusunu işaretleyin).
- **Durdurma:** Actions → "Portföy botu" → *⋯* → *Disable workflow*.
- **Test:** `python test_engine.py` (internet gerektirmez, sahte veriyle senaryoları dener).

> Bu bir simülasyondur; yatırım tavsiyesi değildir.
