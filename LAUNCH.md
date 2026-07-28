# Kemi'yi Hayata Geçirme Rehberi / Going Live

Bu rehber, Kemi'yi kendi makinenizde çalıştırmaktan internete açık bir ağ
işletmeye kadar tüm adımları anlatır. (English summary at the bottom.)

## 1. Localhost — 5 dakikada ilk ağ

```sh
# Kodu alın (zip'i açtıysanız o klasöre girin) ve kurun:
cd Kemi
python3 -m pip install -e .        # opsiyonel hız: python3 -m pip install -e '.[crypto]'

# Her şeyin hazır olduğunu doğrulayın:
kemi doctor

# Tek komutla düğüm + pano (tarayıcı kendiliğinden açılır):
kemi app
```

İkinci bir terminalde ilk işinizi koşun:

```sh
kemi run --task hash.sha256 --input - --lines --peer 127.0.0.1:7700 <<'EOF'
merhaba
dunya
EOF
```

Aynı makinede çok düğümlü küçük bir ağ görmek için: `kemi demo`
(veya rehberli tur: `kemi learn`).

## 1b. Tüm sistemi tek komutla başlatma

Dört bileşeni (önyükleme düğümü, sağlayıcı düğüm, erişim portalı, OpenAI
uyumlu geçit) tek seferde ayağa kaldırmak için:

```sh
scripts/start-stack.sh          # başlat — adresleri ve yönetici anahtarını yazar
scripts/start-stack.sh stop     # hepsini durdur
```

Ortam değişkenleriyle ayarlanır: `BIND` (varsayılan `127.0.0.1`; internete
açmak için `0.0.0.0`), `PORTAL_PORT`, `FAUCET`, `ADMIN_KEY`, `AI_BACKEND`,
`AI_MODEL`, `STACK_HOME`. Gerçek bir modelle yayına almak için:

```sh
BIND=0.0.0.0 AI_BACKEND=ollama AI_MODEL=llama3.2 scripts/start-stack.sh
```

Günlükler `~/.kemi-stack/run/*.log` altındadır. Betik her bileşenin portunu
dinlemeye başlamasını bekler; biri kalkmazsa hata verip durur.

## 2. Evdeki ağ (LAN) — telefon ve diğer bilgisayarlar

- İkinci bilgisayarda: `kemi join` — sihirbaz LAN'daki düğümü çok noktaya
  yayın ile kendiliğinden bulur; bulamazsa ilk makinenin panosundaki
  **Invite a peer** düğmesinden davet kodunu (`kemi1-…`) yapıştırın.
- Telefondan pano: ilk makinede `kemi app --phone` komutunu çalıştırın,
  telefonun tarayıcısıyla gösterilen adresi açın ve **Ana Ekrana Ekle** ile
  PWA olarak kurun.

## 3. AI araçlarınızı ağa bağlama

```sh
kemi serve --peer 127.0.0.1:7700          # OpenAI uyumlu geçit, port 11434
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export OPENAI_API_KEY=kemi                # herhangi bir değer; anahtar istenmez
```

Cursor, Continue, LangChain veya `openai` SDK'sı artık ağ üzerinden çalışır.
Gerçek bir modelle yanıt alabilmek için ağdaki en az bir sağlayıcı düğümde
Ollama kurulu olmalıdır (`kemi node --provide --ai-backend ollama --ai-model llama3.2` gibi);
aksi hâlde deterministik mock arka uç devrededir.

## 4. İnternete açılma — kalıcı bir önyükleme düğümü

Ağın internette buluşma noktası olması için en az bir düğümün erişilebilir
olması yeterlidir (BitTorrent'teki ilk peer gibi; özel bir rolü yoktur):

1. Ucuz bir VPS alın (veya evde sabit IP/port yönlendirmesi yapın) ve
   `7700/tcp` **ve** `7700/udp` portlarını açın.
2. Sunucuda: `pip install -e .` sonra `kemi node --port 7700`.
3. Yeniden başlatmada kendiliğinden kalksın: `kemi service --peer SUNUCU_IP:7700`
   (systemd/launchd birimini üretip kurar; önce görmek için `--dry-run`).
   Alternatif: `docker compose up -d`.
4. Panodan davet kodunu alın ve paylaşın; katılanlar
   `kemi join` sihirbazına bu kodu yapıştırır veya doğrudan
   `kemi node --peer SUNUCU_IP:7700` ile bağlanır.

Ağ büyüdükçe hiçbir düğüm vazgeçilmez değildir: DHT ve dedikodu defteri
sayesinde önyükleme düğümü kapansa bile ağ çalışmaya devam eder.

## 5. Web uygulaması — herkese açık erişim portalı

Kullanıcıların **hiçbir şey kurmadan** tarayıcıdan ağa sorgu göndermesi için:

```bash
# Ağa bağlı bir erişim portalı açın (ziyaretçi başına 10 başlangıç kredisi):
kemi web --peer SUNUCU_IP:7700 --web-port 8090 --faucet 10

# Portalı çalıştıran makine aynı zamanda işlem gücü de paylaşsın:
kemi web --peer SUNUCU_IP:7700 --provide --price 1.0
```

Ziyaretçi `http://SUNUCU_IP:8090/` adresini açar, kendisine özel bir misafir
cüzdanı ile başlangıç kredisi alır ve sorgusunu yazar. Yanıt **merkezî bir
sunucudan değil**, ağdaki sağlayıcı düğümlerden gelir; altında kaynağını
belirten bir damga bulunur:
`1.50 kredi · düğüm silent-coral-14 · model llama3.2`.
Kredisi biten ziyaretçiye kendi düğümünü ağa katması önerilir
(`pip install kemi && kemi app`); ağ bu şekilde büyür. Erişim portalının
ayrıcalıklı bir konumu yoktur: `kemi web` çalıştıran herkes kendi portalını
açabilir; harcamaları o portalı işleten düğüm karşılar.

Kalıcılık: misafir cüzdanları `~/.kemi/portal.json` dosyasında saklanır;
portal yeniden başlatılsa da bakiye ve sohbet geçmişi korunur.

## 6. Yayınlama (isteğe bağlı)

- **GitHub:** `claude/p2p-compute-sharing-wnbm1l` dalını `main`'e birleştirip
  depoyu herkese açık yapın.
- **Sürüm:** `git tag v1.11.0 && git push origin v1.11.0` — CI, üç işletim
  sistemi için tek-dosya uygulamaları derleyip GitHub Release'e ekler
  (`.github/workflows/apps.yml`); PyPI için `RELEASE.md`'deki güvenilir
  yayıncı adımlarını izleyin. Sonrasında kurulum tek satırdır:
  `pip install kemi`.

## Sorun giderme

- `kemi doctor` — makine hazırlık teşhisi (✓/✗ + öneri).
- Portlar: tek port hem TCP hem UDP kullanır; güvenlik duvarında ikisini de açın.
- Durum dosyaları `~/.kemi` altındadır (`KEMI_HOME` ile taşınabilir);
  kimliğinizi (`identity.json`) yedekleyin — düğüm adınız ve bakiyeniz ona bağlıdır.

---

**English:** install with `pip install -e .`, verify with `kemi doctor`, start
with `kemi app`, join from other machines with `kemi join` (LAN auto-discovery
or `kemi1-…` invite codes), expose an always-on bootstrap node by opening
7700/tcp+udp and running `kemi service` (add `--dry-run` to preview the unit), bridge OpenAI-compatible
tools via `kemi serve` (port 11434), and publish by tagging `v1.11.0` so CI
builds the desktop apps.
