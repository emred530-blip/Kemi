# Kemi — P2P İşlem Gücü Paylaşım Ağı

Kemi, **BitTorrent'in dosya paylaşımına yaptığını işlem gücüne yapan** eşler arası (P2P) bir ağdır. Kullanıcılar boştaki CPU/GPU kapasitelerini **kredi karşılığında kiraya verir**; yapay zekâ çıkarımı (inference) veya başka ağır hesaplamalar yapmak isteyenler bu kredilerle **sürünün (swarm) işlem gücünü kiralar**. Amaç, merkezi veri merkezlerine olan bağımlılığı azaltmaktır.

```
                        ┌──────────────┐
                        │   TRACKER    │   eş keşfi + kredi defteri
                        │  (keşif+defter)│  (görev verisi buradan GEÇMEZ)
                        └──────┬───────┘
              kayıt/heartbeat  │  escrow oluştur/çöz
        ┌──────────────┬──────┴───────┬──────────────┐
        │              │              │              │
  ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴─────┐
  │ PROVIDER 1 │  │ PROVIDER 2 │  │ PROVIDER 3 │  │ CONSUMER  │
  │ 0.5 kr/iş  │  │ 1.0 kr/iş  │  │ 1.5 kr/iş  │  │ (istemci) │
  └─────▲─────┘  └─────▲─────┘  └─────▲─────┘  └─────┬─────┘
        │              │              │              │
        └──────────────┴──────────────┴──────────────┘
              parçalar (chunk) doğrudan eşler arasında akar
```

## Nasıl çalışır?

1. **Tracker** (BitTorrent tracker'ı gibi) sürünün buluşma noktasıdır: sağlayıcılar kendini kaydeder ve heartbeat atar, tüketiciler aktif sağlayıcı listesini çeker. Tracker ayrıca **kredi defterini** tutar — görev verisi ve sonuçlar tracker'dan *geçmez*, doğrudan eşler arasında akar.
2. **Sağlayıcı** (`kemi provide`) makinesinin kaynaklarını (CPU, RAM), birim fiyatını ve desteklediği görev türlerini ilan eder; gelen parçaları çalıştırıp kredi kazanır.
3. **Tüketici** (`kemi run`) işini parçalara böler (BitTorrent'teki "piece" mantığı), parçaları tüm uygun sağlayıcılara eşzamanlı dağıtır, başarısız parçaları başka sağlayıcılarda yeniden dener ve sonuçları sırayla birleştirir.
4. **Ödeme escrow ile yapılır:** Tüketici işe başlamadan krediyi tracker'da kilitler ve sağlayıcılara bir *redeem anahtarı* verir. Sağlayıcı her tamamladığı parçanın ücretini bu anahtarla escrow'dan tahsil eder; iş bitince harcanmayan kısım tüketiciye iade edilir. Taraflar birbirine güvenmek zorunda kalmaz.
5. **Sonuç doğrulama (artıklık):** `--redundancy 2+` ile her parça birbirinden bağımsız *farklı* sağlayıcılarda çalıştırılır ve sonuç parmak izleri karşılaştırılır. Uyuşmazlıkta ek bir sağlayıcı hakemlik yapar; **çoğunluk kazanır**. Hatalı veya hileli düğümler böylece etkisiz kalır.
6. **Yeni hesaplara musluk (faucet):** Her yeni düğüm 100 krediyle başlar; kredi kazanmanın yolu işlem gücü paylaşmaktır (BitTorrent'in tit-for-tat ruhu).

## Hızlı başlangıç

Hiçbir bağımlılık gerekmez (yalnızca Python ≥ 3.10 standart kütüphanesi):

```bash
pip install -e .          # veya doğrudan: python3 -m kemi ...

# Tek komutla yerel uçtan uca gösterim (tracker + 3 sağlayıcı + 2 iş):
kemi demo
```

### Gerçek bir sürü kurmak

```bash
# 1. Bir makinede tracker'ı başlat
kemi tracker --port 7700

# 2. İşlem gücünü paylaşacak her makinede
kemi provide --tracker tracker-adresi:7700 --price 0.5 \
             --advertise-host BU_MAKINENIN_IP_ADRESI

# 3. Sürüyü görüntüle
kemi providers --tracker tracker-adresi:7700

# 4. İş gönder: 1000 girdilik hash işini 4'lük parçalara böl,
#    her parçayı 2 farklı sağlayıcıda doğrulat
echo '["a","b","c","d"]' | kemi run --tracker tracker-adresi:7700 \
    --task hash.sha256 --input - --chunk-size 2 --redundancy 2

# 5. Bakiyeni gör
kemi balance --tracker tracker-adresi:7700
```

### Yapay zekâ çıkarımı

`ai.generate` görevi takılabilir backend'lerle çalışır:

```bash
# Bağımlılıksız deterministik mock backend (varsayılan):
kemi provide --tracker ... --ai-backend mock

# Gerçek yerel model (Hugging Face) ile:
pip install "kemi[ai]"
kemi provide --tracker ... --ai-backend transformers
```

```bash
echo '["P2P ağlar neden önemli?"]' | \
    kemi run --tracker ... --task ai.generate --input - --params '{"max_tokens": 64}'
```

## Yerleşik görev türleri

| Görev | Açıklama |
|---|---|
| `ai.generate` | Metin üretimi — istemler (prompt) sürüye dağıtılır |
| `hash.sha256` | Çok turlu SHA-256 (ispat-of-work benzeri yükler) |
| `math.matmul` | Saf Python matris çarpımı (CPU karşılaştırma/benchmark) |
| `text.wordcount` | Kelime/karakter/satır sayımı (map-reduce örneği) |

**Güvenlik modeli:** Sağlayıcılar ağdan gelen rastgele kodu **asla** çalıştırmaz; yalnızca isimle çağrılan, izin listesindeki görevler çalışır. Yeni yetenekler `kemi/tasks.py` içine görev kaydederek eklenir.

## Mimari

| Modül | Sorumluluk |
|---|---|
| `kemi/protocol.py` | Tel protokolü: TCP üzerinde uzunluk önekli JSON mesajlar |
| `kemi/identity.py` | Düğüm kimliği (`node_id = sha256(gizli_token)`) |
| `kemi/tracker.py` | Eş keşfi + kredi defteri sunucusu |
| `kemi/ledger.py` | SQLite destekli kredi defteri ve escrow |
| `kemi/provider.py` | Sağlayıcı düğümü: kayıt, heartbeat, parça yürütme, tahsilat |
| `kemi/consumer.py` | İstemci: parçalama, zamanlama, hata toleransı, çoğunluk doğrulaması |
| `kemi/tasks.py` | İzin listeli görev kayıt defteri |
| `kemi/ai_backends.py` | Takılabilir AI backend'leri (mock / transformers) |
| `kemi/cli.py` | `kemi` komut satırı arayüzü |

## Testler

```bash
python3 -m unittest discover -s tests -v
```

25 test; uçtan uca senaryolar dahil: kredi akışının tutarlılığı, **hileli sağlayıcının çoğunluk oylamasıyla alt edilmesi**, çökmüş sağlayıcının etrafından dolaşılması.

## Yol haritası (MVP'nin bilinçli sınırları)

Bu sürüm çalışan bir MVP'dir; üretime giden yol şu adımlardan geçer:

- **Merkeziyetsiz keşif:** Tracker yerine Kademlia tarzı DHT (BitTorrent'in izlediği yolun aynısı).
- **Merkeziyetsiz defter:** Kredi defterinin tracker'dan çıkarılıp imzalı işlem zincirine taşınması.
- **Kriptografik kimlik:** Token-hash kimliğin Ed25519 anahtar çiftleri ve imzalı mesajlarla değiştirilmesi; TLS.
- **NAT geçişi:** Ev kullanıcıları için UDP hole punching / relay.
- **Sandbox:** Görevlerin container/WASM içinde yalıtılması ve gerçek kaynak kotaları.
- **GPU desteği:** GPU keşfi ve `ai.generate` için GPU'lu backend'ler; büyük modellerin katman-bazlı bölünmesi (pipeline parallelism).
- **İtibar sistemi:** Doğrulama sonuçlarından beslenen sağlayıcı itibar puanları ve fiyat/itibar bazlı zamanlayıcı.
