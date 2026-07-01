# Kemi'yi Hayata Geçirme Rehberi / Going Live

Bu rehber, Kemi'yi kendi makinenizde çalıştırmaktan gerçek bir internet
filosuna kadar tüm adımları verir. (English summary at the bottom.)

## 1. Localhost — 5 dakikada ilk filo

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

Aynı makinede çok düğümlü mini filo görmek için: `kemi demo`
(veya rehberli tur: `kemi learn`).

## 2. Evdeki ağ (LAN) — telefon ve diğer bilgisayarlar

- İkinci bilgisayarda: `kemi join` — sihirbaz LAN'daki düğümü çok noktaya
  yayın ile kendiliğinden bulur; bulamazsa ilk makinenin panosundaki
  **Invite a friend** düğmesinden davet kodunu (`kemi1-…`) yapıştırın.
- Telefondan pano: ilk makinede `kemi app --phone` deyin, telefonun
  tarayıcısıyla gösterilen adresi açın ve **Ana Ekrana Ekle** ile PWA olarak
  kurun.

## 3. AI araçlarınızı filoya bağlama

```sh
kemi serve --peer 127.0.0.1:7700          # OpenAI uyumlu geçit, port 11434
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export OPENAI_API_KEY=kemi                # herhangi bir değer; anahtar istenmez
```

Cursor, Continue, LangChain veya `openai` SDK'sı artık filo üzerinde çalışır.
Gerçek bir modelle yanıt için filodaki en az bir sağlayıcıda Ollama kurulu
olmalı (`kemi node --backend ollama --model llama3.2` gibi); yoksa
deterministik mock arka uç devrededir.

## 4. İnternete açılma — kalıcı bir "liman" düğümü

Filonun internette buluşma noktası olması için en az bir düğümün erişilebilir
olması yeterlidir (BitTorrent'teki ilk peer gibi; özel bir rolü yoktur):

1. Ucuz bir VPS alın (veya evde sabit IP/port yönlendirmesi yapın) ve
   `7700/tcp` **ve** `7700/udp` portlarını açın.
2. Sunucuda: `pip install -e .` sonra `kemi node --port 7700`.
3. Yeniden başlatmada kendiliğinden kalksın: `kemi service --install`
   (systemd/launchd birimi üretir ve kurar). Alternatif: `docker compose up -d`.
4. Panodan davet kodunu alın ve paylaşın; katılanlar
   `kemi join` sihirbazına bu kodu yapıştırır veya doğrudan
   `kemi node --peer SUNUCU_IP:7700` ile bağlanır.

Ağ büyüdükçe hiçbir düğüm vazgeçilmez değildir: DHT ve dedikodu defteri
sayesinde liman düğümü kapansa bile filo yaşamaya devam eder.

## 5. Yayınlama (isteğe bağlı)

- **GitHub:** `claude/p2p-compute-sharing-wnbm1l` dalını `main`'e birleştirip
  depoyu herkese açık yapın.
- **Sürüm:** `git tag v1.5.0 && git push origin v1.5.0` — CI, üç işletim
  sistemi için tek-dosya uygulamaları derleyip GitHub Release'e ekler
  (`.github/workflows/apps.yml`); PyPI için `RELEASE.md`'deki güvenilir
  yayıncı adımlarını izleyin. Sonrasında kurulum tek satırdır:
  `pip install kemi`.

## Sorun giderme

- `kemi doctor` — makine hazırlık teşhisi (✓/✗ + öneri).
- Portlar: tek port hem TCP hem UDP kullanır; güvenlik duvarında ikisini de açın.
- Durum dosyaları `~/.kemi` altındadır (`KEMI_HOME` ile taşınabilir);
  kimliğinizi (`identity.json`) yedekleyin — gemi adınız ve bakiyeniz ona bağlıdır.

---

**English:** install with `pip install -e .`, verify with `kemi doctor`, start
with `kemi app`, join from other machines with `kemi join` (LAN auto-discovery
or `kemi1-…` invite codes), expose an always-on bootstrap node by opening
7700/tcp+udp and running `kemi service --install`, bridge OpenAI-compatible
tools via `kemi serve` (port 11434), and publish by tagging `v1.5.0` so CI
builds the desktop apps.
