# Kemi Master Prompt

Bu dosya, Kemi projesinin TAMAMINI sıfırdan, tek bir prompt ile bir yapay zekâ
kodlama asistanına (Claude Code, Cursor, vb.) yeniden ürettirmek için yazılmış
gelişmiş "master prompt"tur. Aşağıdaki `PROMPT` bölümünü olduğu gibi kopyalayıp
kendi localhost'unuzdaki asistana yapıştırın; boş bir klasörde çalıştırın.

> This file is the single advanced prompt that reproduces the entire Kemi
> project from scratch. Copy the `PROMPT` section verbatim into your local AI
> coding assistant and run it in an empty directory.

---

## PROMPT

Sen kıdemli bir dağıtık-sistemler mühendisisin. Bu klasörde **Kemi** adında,
BitTorrent benzeri, tamamen merkeziyetsiz bir P2P işlem gücü paylaşım ağını
sıfırdan inşa edeceksin. Durup onay sorma; aşağıdaki plana göre kesintisiz
şekilde **yaz → test et → commit'le** döngüsüyle ilerle ve her sürümü ancak tüm
testler yeşilken tamamla.

### Ürün vizyonu

- İnsanlar boştaki CPU/GPU'larını ağa kiralar ve karşılığında **kredi** kazanır;
  kazandıkları krediyle filodaki başka makinelerde ağır işler (AI çıkarımı,
  hash, veri işleme, medya dönüştürme) çalıştırır.
- **Hiçbir merkez yok**: tracker yok, sunucu yok, hesap yok, API anahtarı yok,
  özel rol yok. Her düğüm eşittir; ağ BitTorrent gibi kendi kendine yaşar.
- Tema: düğümler **gemi** (ship) adı taşır, rütbeler Miço'dan Amiral'e yükselir
  (Cabin Boy → Admiral). Ürün dili İngilizce, ama CLI'de tüm komutların Türkçe
  takma adları da olacak (`katil`, `filo`, `bakiye`, `cuzdan`, `parcala`, `ara`,
  `uygulama` …).
- Amiral gemisi özellik: **parçalı büyük model çıkarımı** — tek makinenin
  belleğine sığmayan bir modeli katmanlarına bölüp farklı gemilere dağıtarak
  uçtan uca çalıştırmak.

### Mutlak mühendislik kuralları

1. **Python ≥ 3.10, çekirdek yalnızca stdlib.** Zorunlu üçüncü parti bağımlılık
   yok. `pynacl` (hızlı Ed25519/NaCl) ve `transformers`/`torch` yalnızca
   opsiyonel ekstralar; yoklarsa saf-Python yedekler devreye girer ve testler
   yine geçer.
2. **Önce test.** Her özellik kendi test dosyasıyla gelir; `python3 -m unittest
   discover -s tests` her commit'te yeşil olmalı. Hedef: ~200 test. Ağ testleri
   127.0.0.1 üzerinde gerçek soketlerle koşar; kararsız (flaky) test kabul
   edilme — polling döngüsü + gerekirse bir kez yeniden deneme kalıbı kullan.
   Testlerde PoW zorluğu 4 bit olsun (üretimde 12).
3. **Tek port:** her düğüm aynı port üzerinden hem TCP (iş protokolü) hem UDP
   (DHT) konuşur. Tel protokolü: 4 baytlık big-endian uzunluk öneki + JSON.
   Bozuk/düşmanca girdi asla düğümü çökertmez (fuzz testleriyle kanıtla).
4. **Durum tek yerde:** kimlik, defter ve itibar veritabanları
   `$KEMI_HOME` (varsayılan `~/.kemi`) altında yaşar.
5. Kod yorumları ve dokümantasyon İngilizce; README hem "kodcu olmayanlar" hem
   geliştiriciler için net bir hızlı başlangıç içerir.

### Mimari — inşa edilecek bileşenler

**Kimlik (`kemi/identity.py`, `kemi/crypto.py`)** — Ed25519 anahtar çifti;
düğüm kimliği = açık anahtarın SHA-256'sı, ama geçerli sayılması için
proof-of-work gerekir (kimlik üretirken nonce ara: hash'in ilk
`POW_DIFFICULTY_BITS=12` biti sıfır olmalı; Sybil saldırısını pahalılaştırır).
Saf-Python Ed25519 gerçeklemesi `pynacl` ile bayt-uyumlu olmalı.

**DHT (`kemi/dht.py`)** — UDP üzerinde Kademlia: 160-bit XOR metriği, k-bucket
yönlendirme tablosu, `ping/find_node/store/find_value`, iyileştirici bakım
döngüsü, düğüm başına depolama kotası. Sağlayıcılar yeteneklerini (görev
adları, fiyat, model adı, GPU) imzalı kayıtlar olarak DHT'ye ilan eder;
tüketiciler oradan keşfeder.

**Defter (`kemi/gossip_ledger.py`)** — SQLite destekli, dedikoduyla (gossip)
çoğaltılan CRDT kredi defteri. Her transfer gönderenin Ed25519 imzasını ve
monoton `seq` numarasını taşır; herkes herkese `GENESIS_CREDITS=100` ile
başlar. Aynı `seq` ile iki farklı transfer = **double-spend kanıtı**; kanıt
dedikoduyla yayılır ve hile yapan kalıcı işaretlenir. `history()`, `balance()`,
`total_earned()`, içerik-hash'li **checkpoint/prune** (geçmiş bir taban
çizgisine katlanır, eski transferlerin tekrarı reddedilir, yeni gemiler
snapshot'ı birden çok kaynaktan doğrulayıp hızlı katılır).

**Tanık komiteleri (`witness`)** — anlık double-spend *önleme*: ödeme,
göndericinin anahtarına deterministik olarak yakın, kazanca göre (stake)
ağırlıklı seçilmiş bir komiteye sunulur; yarışan iki ödemeden en fazla biri
onay alır, veto nesnel kanıt taşır.

**İtibar (`kemi/reputation.py`)** — tamamen yerel/öznel Beta-önsel skoru
(good/bad kanıt sayaçları). `decay(factor=0.9)` ile geçici kanıt zamanla
solar (saatte bir), ama `double_spend` **kalıcı mahkûmiyettir** (`condemned`
sütunu; asla af yok). Eski şemadan otomatik migrasyon.

**Uçtan uca şifreleme (`kemi/e2e.py`)** — NaCl `crypto_box` ile bayt-uyumlu
X25519 + XSalsa20-Poly1305 (saf-Python yedeği libsodium'a karşı doğrulanmış).
İş yükleri ve sonuçlar uçtan uca şifreli; röleler yalnızca şifreli metin görür.

**NAT rölesi (`kemi/node.py` içinde)** — TURN-tarzı: NAT arkasındaki
sağlayıcı, açık bir düğümde oturum açar; tüketici trafiği o oturum üzerinden
çok-akışlı (multiplexed) aktarılır; canlı token akışı röle üzerinden de
çalışır. Oturum sayısı sınırlı.

**Koruma (`kemi/ratelimit.py`, `kemi/sandbox.py`)** — IP başına token-bucket
hız sınırı ve bağlantı tavanı (TCP+UDP ayrı ayrı), küresel bağlantı tavanı;
görevler `resource.setrlimit` (CPU süresi, bellek, çıktı boyutu) altında ayrı
süreçte koşar.

**Görevler (`kemi/tasks.py`)** — kayıt defterli görev sistemi: `hash.sha256`,
`text.wordcount`, `data.aggregate`, `crypto.pbkdf2`, `compress.gzip`,
yetenek-kapılı `sci.matmul` (numpy varsa) ve `media.transcode` (ffmpeg varsa;
kabuk yok, format allowlist), `ai.generate`, `ai.embed`, `vector.search`
(kosinüs top-k RAG), `ai.layer` ve `ai.shard`. AI arka uçları
(`kemi/ai_backends.py`): deterministik mock (test), Ollama (stdlib HTTP,
`/api/generate` + `/api/embed`), opsiyonel transformers.

**Yürütme modeli (`kemi/consumer.py`, `kemi/node.py`)** — işler parçalara
(chunk) bölünür, sağlayıcılara dağıtılır; **önce ödeme** (chunk başına imzalı
mikro-ödeme), `redundancy=N` ile aynı parça N farklı gemide koşup oylanır
(deterministik görevlerde bozuk sonuç azınlıkta kalır ve itibar cezası yer);
başarısız parça başka gemide yeniden denenir. Deterministik sonuçlar için
içerik-adresli LRU **sonuç önbelleği** (`ai.generate` asla önbelleklenmez).
Yük arttıkça ilan fiyatı 2x'e kadar süren **dinamik fiyatlandırma** (taban
fiyat her zaman kabul tabanı). Prometheus tarzı `/metrics` + `node.info`
sayaçları.

**Parçalı çıkarım (`kemi/model.py`, `kemi/sharded.py`)** — saf-Python,
deterministik GPT-tarzı transformer referans gerçeklemesi; katmanlar ardışık
gruplara bölünür, her grup `ai.shard` göreviyle FARKLI bir gemiye atanır.
Hiçbir katılımcı modelin tamamını tutmaz; gizli durumlar uçtan uca şifreli
gezer (sağlayıcı yalnız kendi diliminin aktivasyonlarını görür, prompt'u asla).
`ShardedLLM` sürücüsü, `Fleet.shard_generate()`, `kemi shard`/`parcala`.

**Yüksek seviye API (`kemi/api.py`)** — `kemi.connect(peer=...)` → `Fleet`:
`run()`, `generate()`, `stream()`, `embed()`, `rag_search()`,
`shard_generate()`, `run_pipeline()` (boru hattı paralelliği), `balance()`,
`models()`. Üç satırda iş çalıştırılabilmeli.

**OpenAI uyumlu geçit (`kemi/openai_gateway.py`)** — `kemi serve` (port
11434): `/v1/chat/completions` (SSE streaming dahil), `/v1/embeddings`,
`/v1/models` uçlarını filoya köprüler. `OPENAI_BASE_URL` değiştiren her araç
(Cursor, Continue, LangChain, `openai` SDK) Kemi üzerinde çalışır; anahtar yok.

**Pano (`kemi/webui.py`)** — bağımlılıksız gömülü web panosu: sağlayıcılar,
işler, defter, itibar, canlı sohbet (akış + maliyet), bakiye grafiği, tek tık
iş şablonları, **filo haritası** (merkezde bu gemi, halkada sağlayıcılar,
röle bağları kesikli, itibara göre renk), TR/EN dil düğmesi, PWA (manifest +
service worker + saf-Python üretilmiş ikonlar; telefona kurulabilir).
`kemi app --phone` panoyu LAN'a açar.

**Katılım (`kemi/discovery.py`, `kemi/lan.py`, `kemi/invite.py`,
`kemi/names.py`, `kemi/tutorial.py`)** — `kemi join`: 60 saniyelik sihirbaz
(gemi kimliği, çok noktaya yayınla LAN keşfi, paylaş/izle seçimi, pano, davet
kodu). `kemi1-…` davet kodları yalnız bağlantı bilgisi taşır. Deterministik
gemi adları + deftere dayalı rütbeler. `kemi learn`: canlı yerel filoda 3
dakikalık tur.

**CLI (`kemi/cli.py`)** — komutlar (hepsi Türkçe takma adlı): `node`, `app`,
`join`, `run` (`--stream`, `--lines`, `--redundancy`, `--model`), `chat`,
`serve`, `shard`, `search`, `wallet` (bakiye + rütbe + son işlemler),
`providers`, `models`, `status`, `doctor`, `economy`, `service`
(systemd/launchd kurulumu), `demo`, `learn`.

**Paketleme ve operasyon** — `pyproject.toml` (`kemi` konsol betiği; `crypto`
ve `ai` ekstraları), MIT lisansı, `Dockerfile` + `docker-compose.yml`,
`scripts/install.sh` + `scripts/install.ps1`, `packaging/` PyInstaller tarifi,
`launchers/` çift-tık başlatıcıları, GitHub Actions: CI (test matrisi),
release (PyPI), apps (üç OS için PyInstaller derlemesi). `SPEC.md`: başka
dillerde istemci yazmaya yetecek tam protokol belgesi. `CHANGELOG.md`,
`PLATFORMS.md`, `RELEASE.md`.

### Sürüm planı (bu sırayla ilerle, her adımda testler yeşil + commit)

1. **0.1** İzleyicili (tracker) prototip: parçalı işler, emanet kredi,
   fazlalık oylaması, takılabilir AI arka uçları.
2. **0.2** Merkeziyetsizleşme: tracker'ı tamamen sil — DHT, PoW kimlik,
   gossip defteri, itibar, röle, sandbox.
3. **0.3** E2E şifreleme + boru hattı paralelliği. **0.4** Pano.
4. **0.5** Ollama + canlı token akışı + gerçek iş yükleri + 25-düğüm ölçek
   testleri. **0.6** Tanık komiteleri + röleden akış.
5. **0.7–0.8** Katılım sihirbazı, davet kodu, LAN keşfi, gemi adları,
   İngilizce-öncelik + Türkçe takma adlar, lisans/CI.
6. **0.9** Üç satırlık Python API + `chat` + `doctor`. **0.10** DoS koruması +
   defter checkpoint/prune + panoda sohbet.
7. **0.11** Model pazarı + `ai.embed` + protokol fuzz'ı. **0.12** Parçalı
   büyük model çıkarımı. **0.13** `kemi app` + başlatıcılar.
8. **0.14** Sonuç önbelleği, metrikler, dinamik fiyat, `service`, Docker,
   SPEC. **0.15** Stake-ağırlıklı tanıklar + ekonomi simülasyonu + iş
   şablonları.
9. **1.0** Kararlı sürüm. **1.1** PWA/mobil. **1.2** RAG + iki dilli pano +
   indirilebilir uygulamalar. **1.3** Filo haritası + medya dönüştürme +
   `search`. **1.4** OpenAI geçidi. **1.5** `wallet` + itibar solması
   (double-spend affı yok) + `$KEMI_HOME`.

### Kabul ölçütü

`python3 -m unittest discover -s tests` tamamen yeşil (~200 test); tek
makinede `kemi demo` uçtan uca çalışır; iki terminalde `kemi node` +
`kemi run hash.sha256 ...` gerçek iş koşar; `kemi serve` altında `openai`
SDK'sı sohbet tamamlaması alır. Her sürüm CHANGELOG'a işlenir.

---

*Prompt sonu. Bu dosyanın üstündeki `PROMPT` bölümünü kopyalayıp asistanına
ver; gerisini o halleder.*
