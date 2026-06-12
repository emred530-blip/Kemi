# Kemi — Merkeziyetsiz P2P İşlem Gücü Paylaşım Ağı

Kemi, **BitTorrent'in dosya paylaşımına yaptığını işlem gücüne yapan**, tamamen merkeziyetsiz bir eşler arası (P2P) ağdır. Kullanıcılar boştaki CPU/GPU kapasitelerini **kredi karşılığında kiraya verir**; yapay zekâ çıkarımı (inference) veya başka ağır hesaplamalar yapmak isteyenler bu kredilerle **sürünün (swarm) işlem gücünü kiralar**. Amaç, merkezi veri merkezlerine olan bağımlılığı azaltmaktır.

**v0.2'den beri ağda hiçbir merkezi bileşen yoktur** — tracker yok, defter sunucusu yok, özel rol yok; her katılımcı aynı `kemi node`'u çalıştırır. **v0.3**, tüm iş trafiğini uçtan uca şifreler ve katman-parçalı modeller için pipeline paralelliğini ekler. **v0.4**, sürüyü tarayıcıdan izleyip yönetebileceğiniz gömülü canlı web panelini getirir. **v0.5**, Ollama ile **gerçek LLM çıkarımını**, sürü üzerinden **canlı token akışını (streaming)**, gerçek iş yüklerini ve 25-düğümlü ölçek/churn testlerini ekler.

```
              ╭────────────────  KEŞİF: Kademlia DHT (UDP)  ────────────────╮
              │   sağlayıcı kayıtları görev-anahtarlarının XOR-en-yakın     │
              │   K düğümünde tutulur; imzalı ve kısa ömürlüdür             │
              ╰─────────────────────────────────────────────────────────────╯
   ┌──────────┐      ┌──────────┐      ┌──────────┐      ┌──────────┐
   │  EŞ  A   │◄────►│  EŞ  B   │◄────►│  EŞ  C   │◄────►│  EŞ  D   │
   │ sağlayıcı│      │ tüketici │      │ sıradan  │      │ sağlayıcı│
   │ 0.5 cr/iş│      │          │      │ eş+relay │◄═════│ (NAT     │
   └────▲─────┘      └────┬─────┘      └──────────┘ kalıcı│ arkasında│
        │   parçalar +    │                        bağlantı└──────────┘
        ╰── imzalı ödeme ─╯
              ╭─────────────────────────────────────────────────────────────╮
              │  DEFTER: imzalı transferlerin dedikodu (gossip) ile         │
              │  çoğaltılan CRDT kümesi — her eşte tam bir replika          │
              ╰─────────────────────────────────────────────────────────────╯
```

## Merkeziyetsizlik nasıl sağlanıyor?

| Sorun | Çözüm |
|---|---|
| **Eş keşfi** | Kademlia DHT (BitTorrent'in trackersız modu, BEP 5 ile aynı yaklaşım). Sağlayıcılar imzalı kayıtlarını görev başına türetilen anahtarların altında, XOR-en-yakın K düğümde yayımlar; kayıtlar TTL ile kendiliğinden eskir. Ağa katılmak için herhangi bir çalışan eş yeterlidir. |
| **Kimlik** | Ed25519 anahtar çifti. `node_id = sha256(pubkey ‖ nonce)` ve baştan N biti sıfır olmak zorunda: kimlik basmak **proof-of-work** gerektirir, bu da Sybil saldırılarını ve bedava-kredi (faucet) istismarını pahalılaştırır. PyNaCl varsa libsodium, yoksa saf-Python RFC 8032 kullanılır. |
| **Ödeme** | Escrow/tracker yerine **parça başına Ed25519-imzalı kredi transferi** doğrudan sağlayıcıya verilir. Defter, imzalı işlemlerin *büyüme-tek-yönlü kümesidir* (CRDT): dedikoduyla çoğalır, varış sırasından bağımsız olarak her replika aynı duruma yakınsar. |
| **Çift harcama** | Her gönderici işlemlerini artan `seq` ile numaralandırır. Aynı `(gönderici, seq)` ile iki farklı işlem = matematiksel kanıt: ikisi de delil olarak saklanır, deterministik olan teki sayılır ve hesap **kalıcı olarak işaretlenir**; dürüst düğümler hizmet vermeyi keser. |
| **Sahte sonuç** | `--redundancy 2+`: her parça birbirinden bağımsız farklı sağlayıcılarda çalışır, sonuç parmak izleri karşılaştırılır, **çoğunluk kazanır**. Kaybedenler yerel itibar cezası yer. |
| **İtibar** | Her düğüm yalnızca *birinci elden* deneyimden beslenen yerel (öznel) puan tutar — paylaşılan itibar kolay zehirlenir, birinci el deneyim zehirlenemez. Çift harcama kanıtı ise nesneldir ve işlemlerle birlikte kendisi yayılır. |
| **NAT geçişi** | Her yanıt, isteği yapanın *gözlemlenen* dış adresini geri söyler (STUN'a gerek kalmaz). NAT arkasındaki sağlayıcı, herhangi bir erişilebilir eşe kalıcı bağlantı açar ve görev trafiği oradan **relay** edilir (TURN benzeri). |
| **Yalıtım** | Görevler izin listelidir (ağdan asla rastgele kod çalıştırılmaz) ve buna ek olarak her parça, CPU-saniye / bellek / dosya tanıtıcısı sınırlı (`rlimit`) ayrı bir süreçte çalışır. |
| **Gizlilik** | Parça içerikleri ve sonuçlar tüketici ile sağlayıcı arasında **uçtan uca şifrelidir** (NaCl `crypto_box` ile birebir uyumlu: X25519 + XSalsa20-Poly1305). Anahtarlar mevcut Ed25519 kimliklerden türetilir — el sıkışma gerekmez; relay'ler yalnızca şifreli metin görür. PyNaCl yoksa saf-Python uygulama devreye girer; ikisi bayt-bayt aynı çıktıyı üretir (libsodium'a karşı testli). |
| **Büyük modeller** | **Pipeline paralelliği**: `run_pipeline` ile bir aşamanın çıktısı sonraki aşamanın girdisi olur; katmanlara bölünmüş bir model, hiçbiri modelin tamamını barındıramayan sağlayıcılar üzerinde uçtan uca koşabilir. Her aşama tam zamanlayıcı muamelesi görür (parçalama, yeniden deneme, çoğunluk doğrulaması, imzalı ödeme, şifreleme). |
| **GPU** | `nvidia-smi` ile GPU keşfi yapılır ve kaynak ilanında yayımlanır; `transformers` backend'i GPU'da koşabilir. |

### Güven modeli (dürüst özet)

Ödeme parça istekleriyle birlikte gittiği için kötü niyetli bir sağlayıcının çalabileceği tutar **bir parçanın fiyatıyla sınırlıdır** — BitTorrent'in küçük parçalarla riski sınırlaması gibi. Defter anlık kesinlik (finality) yerine **nihai tutarlılık** sunar: hile dedikodu yayılınca kesin olarak yakalanır ve hesap yakılır. İtibar + PoW kimlik maliyeti, tekrarlanan saldırıyı ekonomik olarak anlamsızlaştırır. (Yol haritası: pay-ağırlıklı çekirdek imzalarıyla sert kesinlik.)

## Hızlı başlangıç

Zorunlu bağımlılık yok (Python ≥ 3.10 standart kütüphanesi yeter; `pynacl` önerilir):

```bash
pip install -e .            # hızlı imza için: pip install -e ".[crypto]"

kemi demo                   # tek komutla yerel merkeziyetsiz sürü gösterimi
kemi demo --ui 8080         # demo sonrası sürü ayakta kalır; canlı panel:
                            #   http://127.0.0.1:8080/
```

Demo tek süreçte şunları kurar ve kanıtlar: bootstrap eşi, farklı fiyatlı dürüst sağlayıcılar, **NAT arkasında relay'le çalışan** bir sağlayıcı, **hileli** bir sağlayıcı ve bir tüketici. Hileli çoğunluk oylamasıyla elenir + yasaklanır ve iş bitince **bütün replikaların aynı bakiyelere yakınsadığı** gösterilir.

### Gerçek bir sürü kurmak

```bash
# 1. İlk eşi başlat (hiçbir özel rolü yok; sadece ilk olan o)
kemi node --port 7700

# 2. İşlem gücü paylaşacak her makinede
kemi node --provide --peer ILK_ESIN_IP:7700 --price 0.5
#    NAT arkasındaysanız: --force-relay  (otomatik tespit de denenir)

# 3. Sürüyü görüntüle
kemi providers --peer ILK_ESIN_IP:7700

# 4. İş gönder: parçalara böl, 2 farklı sağlayıcıda çapraz doğrula
echo '["a","b","c","d"]' | kemi run --peer ILK_ESIN_IP:7700 \
    --task hash.sha256 --input - --chunk-size 2 --redundancy 2

# 5. Çok aşamalı pipeline işi (katman-parçalı model çalıştırmanın temeli)
echo '[[0.1,0.2,0.3]]' | kemi pipeline --peer ILK_ESIN_IP:7700 --input - \
    --stages '[{"task":"ai.layer","params":{"layer":0}},{"task":"ai.layer","params":{"layer":1}}]'

# 6. Bakiye, kimlik ve eş sağlığı
kemi balance --peer ILK_ESIN_IP:7700
kemi id
kemi status --peer ILK_ESIN_IP:7700

# 7. Canlı web paneli: sağlayıcılar, defter, itibar + tarayıcıdan iş gönderme
kemi node --peer ILK_ESIN_IP:7700 --ui 8080   # http://127.0.0.1:8080/
```

Tüm iş trafiği varsayılan olarak **uçtan uca şifrelidir** (sağlayıcı kaydı `e2e` yeteneğini ilan eder; `Job(encrypt=False)` ile kapatılabilir).

### Yapay zekâ çıkarımı (gerçek modellerle)

```bash
# Bağımlılıksız deterministik mock backend (varsayılan):
kemi node --provide --peer ... --ai-backend mock

# GERÇEK yerel model — Ollama ile (önerilen yol):
#   1) https://ollama.com adresinden Ollama'yı kur
#   2) ollama pull llama3.2
#   3) işlem gücünü modele aç:
kemi node --provide --peer ... --ai-backend ollama --ai-model llama3.2

# Alternatif: Hugging Face pipeline süreç-içi (GPU varsa keşfedilir):
pip install "kemi[ai]"
kemi node --provide --peer ... --ai-backend transformers
```

```bash
# Toplu üretim:
echo '["P2P ağlar neden önemli?"]' | \
    kemi run --peer ... --task ai.generate --input - --params '{"max_tokens": 64}'

# CANLI akış: tokenlar model ürettikçe ekranına düşer (uçtan uca şifreli):
echo '["P2P ağlar neden önemli?"]' | \
    kemi run --peer ... --task ai.generate --input - --stream
```

Akış, ödemenin *önce* alındığı tek yoldur (aksi hâlde tüketici son token'dan sonra kaçabilirdi); maruziyet yine tek parça fiyatıyla sınırlıdır ve akış yapan sağlayıcılar kayıtlarında `stream` rozetini ilan eder. Panel, `ai.generate` işlerinde model çıktısını gerçek zamanlı büyürken gösterir.

### Canlı web paneli

`--ui PORT` ile her düğüm, bağımlılıksız bir kontrol paneli sunar:

- **Sağlayıcılar (canlı):** fiyat, itibar puanı, CPU/GPU, direkt/relay yolu, e2e rozeti — yasaklı/işaretli düğümler otomatik elenmiş hâlde.
- **İş gönderme:** görev seç, JSON öğeleri yapıştır, parça boyutu/artıklık ayarla; iş sürüde koşarken durumunu ve maliyetini tablodan izle.
- **Defter:** bakiye, son transferler ve varsa çift harcama kanıtları (⚑).
- **İtibar:** bu düğümün gözünden eş puanları.

Panel varsayılan olarak yalnızca `127.0.0.1`'e bağlanır (kimlik doğrulaması yoktur; dışarı açacaksanız güvendiğiniz bir ters vekilin arkasına koyun). Sayfa 2 saniyede bir kendini yeniler.

## Yerleşik görev türleri

| Görev | Açıklama | Sandbox |
|---|---|---|
| `ai.generate` | Metin üretimi (mock/Ollama/transformers; canlı akış desteği) | süreç-içi (model belleği) |
| `ai.layer` | Katman-parçalı model şeridi (pipeline paralelliği) | ✓ |
| `data.aggregate` | Map-reduce: JSON kayıtlarda grupla + topla/ortalama/min/max/say | ✓ |
| `crypto.pbkdf2` | PBKDF2-HMAC-SHA256 anahtar sertleştirme (gerçek CPU yükü) | ✓ |
| `compress.gzip` | Toplu sıkıştırma (metin veya base64 ikili) | ✓ |
| `sci.matmul` | Gerçek BLAS matris çarpımı — *numpy kuruluysa otomatik ilan edilir* | ✓ |
| `hash.sha256` | Çok turlu SHA-256 | ✓ |
| `math.matmul` | Saf Python matris çarpımı (benchmark) | ✓ |
| `text.wordcount` | Kelime/karakter/satır sayımı | ✓ |

`sci.matmul` örneği, **yeteneğe bağlı görev** desenidir: görev yalnızca bağımlılığı bulunan sağlayıcılarda kaydolur ve yalnızca onların DHT kayıtlarında ilan edilir — ffmpeg/video kodlama gibi ağır yükler de aynı desenle eklenir.

Yeni yetenekler `kemi/tasks.py` içine görev kaydederek eklenir; güvenlik sınırı her zaman isim-bazlı izin listesidir.

## Mimari

| Modül | Sorumluluk |
|---|---|
| `kemi/crypto.py` | Ed25519 (PyNaCl → saf-Python yedeği), kanonik JSON, imzalı zarflar |
| `kemi/identity.py` | PoW destekli anahtar çifti kimliği |
| `kemi/dht.py` | Kademlia DHT: k-bucket'lar, yinelemeli arama, imzalı+TTL'li kayıtlar, gözlemlenen-adres (NAT tespiti) |
| `kemi/discovery.py` | DHT üstünde sağlayıcı ilanı/keşfi |
| `kemi/e2e.py` | Uçtan uca şifreleme: NaCl box uyumlu X25519 + XSalsa20-Poly1305 (saf-Python yedekli) |
| `kemi/gossip_ledger.py` | İmzalı işlem CRDT'si: dedikodu çoğaltması, çift harcama kanıtı, seq rezervasyonu |
| `kemi/reputation.py` | Yerel itibar puanları (Beta tahmini) ve yasaklama |
| `kemi/sandbox.py` | rlimit'li alt süreç yalıtımı |
| `kemi/node.py` | Birleşik eş: TCP servisleri, gossip döngüleri, sağlayıcı hizmeti, relay (iki taraf), GPU keşfi |
| `kemi/consumer.py` | Parçalama, zamanlama, hata toleransı, çoğunluk doğrulaması, parça başına imzalı ödeme |
| `kemi/protocol.py` | TCP tel protokolü: uzunluk önekli JSON |
| `kemi/tasks.py`, `kemi/ai_backends.py` | İzin listeli görevler, takılabilir AI backend'leri |
| `kemi/webui.py` | Gömülü canlı web paneli (stdlib HTTP; sağlayıcılar, defter, itibar, iş gönderme) |
| `kemi/cli.py`, `kemi/demo.py` | Komut satırı ve uçtan uca gösterim |

## Testler

```bash
python3 -m unittest discover -s tests -v
```

87 test: 25 düğümlü sürü ölçeği, iş ortasında sağlayıcıların yarısının ölmesi (churn), disk üzerinden yeniden başlatma/seq güvenliği, canlı token akışı (tel üzerinde düz metin sızmadığının kanıtıyla), sahte Ollama sunucusuna karşı backend doğrulaması, kripto çapraz-backend birlikte çalışabilirliği (saf-Python NaCl uygulaması libsodium'a karşı bayt-bayt doğrulanır; RFC 7748/8439 test vektörleri), PoW kimlik, DHT depolama/arama, defter yakınsaması ve çift harcama kanıtı, itibar/yasaklama, sandbox, relay üzerinden NAT'lı sağlayıcı, relay'in yalnızca şifreli metin gördüğünün kanıtı, pipeline kompozisyonu ve hileli sağlayıcının çoğunlukla alt edilmesi dahil uçtan uca sürü senaryoları.

## Yol haritası

- **Sert kesinlik:** Pay-ağırlıklı çekirdek (quorum) makbuzlarıyla işlem kesinliği; defterin dönemsel özetlerle (checkpoint) budanması.
- **Tam delik açma:** Relay'e ek olarak UDP hole-punching ile NAT'lar arası doğrudan görev trafiği.
- **Daha sert yalıtım:** Container/WASM çalıştırıcı, dosya sistemi ve ağ ad alanları, gerçek GPU kotaları.
- **Gerçek model şeritleri:** `ai.layer`'ın referans uygulamasının yerine gerçek transformer katman gruplarını koyan bir backend (pipeline altyapısı hazır).
- **Relay üzerinden akış:** Streaming şu an doğrudan bağlantı ister; relay oturumları üzerinden çoklanmış (multiplexed) akış.
