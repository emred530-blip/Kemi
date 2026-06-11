# Kemi — Merkeziyetsiz P2P İşlem Gücü Paylaşım Ağı

Kemi, **BitTorrent'in dosya paylaşımına yaptığını işlem gücüne yapan**, tamamen merkeziyetsiz bir eşler arası (P2P) ağdır. Kullanıcılar boştaki CPU/GPU kapasitelerini **kredi karşılığında kiraya verir**; yapay zekâ çıkarımı (inference) veya başka ağır hesaplamalar yapmak isteyenler bu kredilerle **sürünün (swarm) işlem gücünü kiralar**. Amaç, merkezi veri merkezlerine olan bağımlılığı azaltmaktır.

**v0.2 itibarıyla ağda hiçbir merkezi bileşen yoktur** — tracker yok, defter sunucusu yok, özel rol yok. Her katılımcı aynı `kemi node`'u çalıştırır.

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
| **GPU** | `nvidia-smi` ile GPU keşfi yapılır ve kaynak ilanında yayımlanır; `transformers` backend'i GPU'da koşabilir. |

### Güven modeli (dürüst özet)

Ödeme parça istekleriyle birlikte gittiği için kötü niyetli bir sağlayıcının çalabileceği tutar **bir parçanın fiyatıyla sınırlıdır** — BitTorrent'in küçük parçalarla riski sınırlaması gibi. Defter anlık kesinlik (finality) yerine **nihai tutarlılık** sunar: hile dedikodu yayılınca kesin olarak yakalanır ve hesap yakılır. İtibar + PoW kimlik maliyeti, tekrarlanan saldırıyı ekonomik olarak anlamsızlaştırır. (Yol haritası: pay-ağırlıklı çekirdek imzalarıyla sert kesinlik.)

## Hızlı başlangıç

Zorunlu bağımlılık yok (Python ≥ 3.10 standart kütüphanesi yeter; `pynacl` önerilir):

```bash
pip install -e .            # hızlı imza için: pip install -e ".[crypto]"

kemi demo                   # tek komutla yerel merkeziyetsiz sürü gösterimi
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

# 5. Bakiye ve kimlik
kemi balance --peer ILK_ESIN_IP:7700
kemi id
```

### Yapay zekâ çıkarımı

```bash
# Bağımlılıksız deterministik mock backend (varsayılan):
kemi node --provide --peer ... --ai-backend mock

# Gerçek yerel model (Hugging Face; GPU varsa keşfedilir):
pip install "kemi[ai]"
kemi node --provide --peer ... --ai-backend transformers
```

```bash
echo '["P2P ağlar neden önemli?"]' | \
    kemi run --peer ... --task ai.generate --input - --params '{"max_tokens": 64}'
```

## Yerleşik görev türleri

| Görev | Açıklama | Sandbox |
|---|---|---|
| `ai.generate` | Metin üretimi — istemler sürüye dağıtılır | süreç-içi (model belleği) |
| `hash.sha256` | Çok turlu SHA-256 | ✓ |
| `math.matmul` | Saf Python matris çarpımı (benchmark) | ✓ |
| `text.wordcount` | Kelime/karakter/satır sayımı | ✓ |

Yeni yetenekler `kemi/tasks.py` içine görev kaydederek eklenir; güvenlik sınırı her zaman isim-bazlı izin listesidir.

## Mimari

| Modül | Sorumluluk |
|---|---|
| `kemi/crypto.py` | Ed25519 (PyNaCl → saf-Python yedeği), kanonik JSON, imzalı zarflar |
| `kemi/identity.py` | PoW destekli anahtar çifti kimliği |
| `kemi/dht.py` | Kademlia DHT: k-bucket'lar, yinelemeli arama, imzalı+TTL'li kayıtlar, gözlemlenen-adres (NAT tespiti) |
| `kemi/discovery.py` | DHT üstünde sağlayıcı ilanı/keşfi |
| `kemi/gossip_ledger.py` | İmzalı işlem CRDT'si: dedikodu çoğaltması, çift harcama kanıtı, seq rezervasyonu |
| `kemi/reputation.py` | Yerel itibar puanları (Beta tahmini) ve yasaklama |
| `kemi/sandbox.py` | rlimit'li alt süreç yalıtımı |
| `kemi/node.py` | Birleşik eş: TCP servisleri, gossip döngüleri, sağlayıcı hizmeti, relay (iki taraf), GPU keşfi |
| `kemi/consumer.py` | Parçalama, zamanlama, hata toleransı, çoğunluk doğrulaması, parça başına imzalı ödeme |
| `kemi/protocol.py` | TCP tel protokolü: uzunluk önekli JSON |
| `kemi/tasks.py`, `kemi/ai_backends.py` | İzin listeli görevler, takılabilir AI backend'leri |
| `kemi/cli.py`, `kemi/demo.py` | Komut satırı ve uçtan uca gösterim |

## Testler

```bash
python3 -m unittest discover -s tests -v
```

47 test: kripto çapraz-backend birlikte çalışabilirliği, PoW kimlik, DHT depolama/arama, defter yakınsaması ve çift harcama kanıtı, itibar/yasaklama, sandbox, relay üzerinden NAT'lı sağlayıcı ve hileli sağlayıcının çoğunlukla alt edilmesi dahil uçtan uca sürü senaryoları.

## Yol haritası

- **Sert kesinlik:** Pay-ağırlıklı çekirdek (quorum) makbuzlarıyla işlem kesinliği; defterin dönemsel özetlerle (checkpoint) budanması.
- **Tam delik açma:** Relay'e ek olarak UDP hole-punching ile NAT'lar arası doğrudan görev trafiği.
- **Daha sert yalıtım:** Container/WASM çalıştırıcı, dosya sistemi ve ağ ad alanları, gerçek GPU kotaları.
- **Büyük model paralelliği:** `ai.generate`'in katman-bazlı (pipeline) bölünerek tek başına sığmayan modellerin sürüde koşturulması.
- **Şifreli iş yükleri:** Uçtan uca şifreli parça içerikleri; relay hiçbir şey okuyamasın.
