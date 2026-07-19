# H3 — Ön-Kayıtlı Test Planı
## Coinbase Order-Flow (CVD) Sinyali → Polymarket 5m Up/Down İcrası

**Kayıt tarihi:** 2026-07-19 (veri görülmeden önce yazıldı)

## Hipotez

Coinbase BTC/ETH trade tape'inden hesaplanan 10s rolling CVD, spot mid-price'ın
sonraki 5-15 saniyesini öngörüyor (H1'de 37 saatlik veriyle doğrulandı: BTC t=9.59,
ETH t=3.90, HAC-corrected, işaret pozitif). H3 sorusu: **bu sinyal, Polymarket 5m
Up/Down market'lerinin pencere-sonu fiyatlarında, fee-dahil pozitif beklenen değerle
icra edilebilir mi?**

Mekanizma adayı: pencerenin son ~30-60 saniyesinde market fiyatı spot'un "open'a
göre yönü"ne kilitlenir. Spot dönüşü 5-15sn önceden bilinirse, ölmek üzere görünen
taraf ucuza toplanabilir (0x50f7'nin gözlemlenen deseninin bilgili versiyonu).

## Doğrulanmış fee modeli (2026-07 itibarıyla)

- Kripto kategori taker fee: `fee = shares × 0.07 × p × (1-p)` (Temmuz 2026'da 0.072→0.07'ye indi)
- Maker: 0 fee + rebate programı
- p=0.10'da fee ≈ notional'ın %0.63'ü; p=0.50'de ≈ %3.5'i
- KURAL: tüm EV hesapları taker varsayımıyla (kötümser) yapılır; maker fill'i bonus sayılır

## Toplanacak veri

1. Coinbase tarafı: mevcut collector (trades + quotes) — ZATEN ÇALIŞIYOR, dokunulmaz
2. Polymarket tarafı: YENİ collector (poly_collector.py) — aktif BTC/ETH 5m updown
   market'inin Up token'ı için her ~1.5sn'de best bid/ask + mid (CLOB /book), aynı
   Postgres'e `poly_quotes` tablosuna. Pencere geçişlerinde otomatik market keşfi
   (slug = `{asset}-updown-5m-{300sn'ye hizalı epoch}`, Gamma API ile token id çözümü).

## Ön-kayıtlı karar kapısı

Veri toplandıktan sonra, kronolojik split (ilk yarı = arama, ikinci yarı = doğrulama):

**Sinyal tanımı (arama yarısında serbestçe optimize edilebilir, doğrulama yarısında DONDURULUR):**
- CVD eşiği, pencere-içi zaman filtresi (örn. son 60sn), fiyat bandı filtresi

**Doğrulama yarısında GEÇME koşulları (hepsi birden):**
1. Fee-dahil (taker, 0.07 formülü) ortalama PnL > 0, HAC/bootstrap t > 2.5
2. n ≥ 300 bağımsız sinyal olayı (T7 standardı)
3. Sinyal anında Polymarket'te gerçekten satın alınabilir fiyat vardı (best ask
   kaydına karşı, mid'e değil) — lookahead yasak: CVD(t) sadece t'ye kadarki
   Coinbase verisi, giriş fiyatı t+1sn'deki poly best ask
4. Edge, "her zaman ucuz tarafı al" naive baseline'ından anlamlı şekilde iyi
   (bilgisiz versiyon zaten kârlıysa sinyalin katkısı ayrıştırılmalı)

**KAPANMA koşulu:** Doğrulama yarısında koşullardan herhangi biri sağlanmazsa hat
kapanır, rapor yazılır. "Biraz daha optimize edelim" YOK.

## Bilinen riskler (şimdiden kayda geçen)

- Polymarket best ask, sinyal anında zaten dönmüş olabilir (bu market'lerde bot
  yoğunluğu yüksek — 8 wallet analizi bunu gösterdi). Test tam da bunu ölçüyor.
- REST polling ~1.5sn çözünürlük; gerçek icra gecikmesi bunun üstüne biner.
  Paper-test PnL'i bu yüzden iyimser taraflı olabilir → gate geçse bile canlıya
  geçmeden küçük-boyutlu canlı doğrulama şart.
- 5m market'lerde 0.07 fee oranının 15m'den farklı olma ihtimali — ilk toplanan
  gerçek fill'lerde/dokümanda tekrar doğrulanacak.
