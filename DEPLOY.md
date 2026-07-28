# Kemi Sunucu Kurulumu / Server Deployment

Bu rehber, Kemi'yi kendi alan adınız altında, TLS ile, yeniden başlatmaya
dayanıklı biçimde yayına almayı anlatır. `LAUNCH.md` "nasıl çalıştırılır"
sorusunu; bu belge "internete nasıl açılır" sorusunu yanıtlar.
(English summary at the bottom.)

Sonuçta elinize geçen:

| Adres | Nedir |
| --- | --- |
| `https://alanadiniz.com/` | Erişim portalı — ziyaretçilerin sorgu yazdığı web uygulaması |
| `https://alanadiniz.com/admin` | Operatör konsolu — bakiye, kullanıcılar, sağlayıcı kalitesi |
| `https://alanadiniz.com/v1` | OpenAI uyumlu API — mevcut yapay zekâ araçlarınız için |
| `https://alanadiniz.com/healthz` | Canlılık kontrolü — izleme servisleri için |
| `alanadiniz.com:7700` | Düğümün eşler arası portu — ağa katılanların bağlandığı yer |

---

## 1. Gereken şeyler

- Debian veya Ubuntu çalıştıran bir sunucu (1 vCPU / 1 GB yeterlidir; gerçek
  bir model sunacaksanız 8 GB RAM ve mümkünse bir GPU).
- Sunucunun IP adresine yönlendirilmiş bir alan adı (A kaydı).
- Açık portlar: `80/tcp`, `443/tcp` ve düğüm portu `7700` — **hem tcp hem
  udp**. UDP açık değilse düğüm keşfedilmez, ağ sizi görmez.

```sh
sudo ufw allow 80,443/tcp
sudo ufw allow 7700/tcp
sudo ufw allow 7700/udp
```

## 2. Tek komutla kurulum

```sh
git clone https://github.com/emred530-blip/kemi.git
cd kemi
sudo ./deploy/install-server.sh --domain alanadiniz.com --tls --email siz@ornek.com
```

Betik sırasıyla şunları yapar; hepsi geri alınabilir:

1. Ayrıcalıksız `kemi` sistem kullanıcısını ve `/var/lib/kemi` dizinini oluşturur.
2. Kemi'yi kendi sanal ortamına (`/opt/kemi/venv`) kurar.
3. `/etc/kemi/kemi.env` dosyasını yazar ve **rastgele bir yönetici anahtarı
   üretir** (dosya zaten varsa dokunmaz).
4. Üç systemd birimini kurar ve başlatır: `kemi-node`, `kemi-portal`,
   `kemi-gateway`.
5. nginx sitesini yazar ve `--tls` verildiyse certbot ile sertifika alır.

Bitince yönetici anahtarını ekrana yazar. Kaydedin — konsola girişin tek yolu odur.

Betiği tekrar çalıştırmak güvenlidir: kodu ve birimleri günceller, ayar
dosyanıza ve kazandığınız krediye dokunmaz.

### Ters vekil sunucu istemiyorsanız

```sh
sudo ./deploy/install-server.sh --no-nginx
```

Servisler kurulur ama portal yalnızca `127.0.0.1:8090` üzerinde dinler.
Kendi vekil sunucunuzu koyana kadar dışarıdan erişilemez — bu kasıtlıdır.

### Var olan bir ağa katılmak

İlk sunucunuz kendi başına bir giriş noktasıdır. Zaten çalışan bir ağa
bağlanacaksanız:

```sh
sudo ./deploy/install-server.sh --domain alanadiniz.com --tls \
     --peer mevcut-sunucu.com:7700
```

## 3. Ayarlar

Her şey `/etc/kemi/kemi.env` içindedir. Değiştirdikten sonra:

```sh
sudo systemctl restart kemi-node kemi-portal kemi-gateway
```

En çok dokunacağınız üç ayar:

```ini
# Gerçek bir model sunun — 'mock' yalnızca kurulum testi içindir.
KEMI_AI_BACKEND=ollama
KEMI_AI_MODEL=llama3.2

# Her yeni ziyaretçiye verilen başlangıç kredisi (sizin bakiyenizden çıkar)
# ve bir adresin saatte kaç kez bu krediyi alabileceği.
KEMI_FAUCET=10
KEMI_GUESTS_PER_IP=5

# Operatör konsolu anahtarı.
KEMI_ADMIN_KEY=...
```

`ollama` kullanacaksanız onu ayrıca kurup modeli indirin
(`curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3.2`),
sonra `kemi-node` servisini yeniden başlatın.

## 4. Çalıştığını doğrulama

```sh
systemctl status kemi-node kemi-portal kemi-gateway
curl -s https://alanadiniz.com/healthz            # {"ok": true, ...}
journalctl -u kemi-portal -f                      # canlı günlük
```

Tarayıcıdan portalı açın, bir soru sorun ve yanıtın altındaki damgaya bakın:
`1.00 kredi · düğüm silent-coral-14 · model llama3.2`. Damgadaki düğüm adı
sorunun **kimin makinesinde** yanıtlandığını söyler — merkezî bir sunucu yoktur.

API'yi denemek için:

```sh
curl https://alanadiniz.com/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"kemi","messages":[{"role":"user","content":"merhaba"}]}'
```

## 5. Neyin nasıl korunduğu

Herkese açık bir portal iki şeyi kaybedebilir: kredi ve kontrol. İkisi de
savunmaya alınmıştır.

**Kredi.** Başlangıç kredisi gerçek paradır ve oturum jetonu tarayıcının
söylediği şeydir; kimlik doğrulaması yoktur. Bu yüzden portal, yeni ve dolu
bir hesabın açılmasını adres başına saatlik olarak sınırlar
(`KEMI_GUESTS_PER_IP`). Sınıra takılan istek `429` alır; **elinde jeton olan
ziyaretçi asla geri çevrilmez**. Aynı adresten açılmış hesaplar konsolda ortak
bir *köken* etiketiyle görünür — adres hiçbir yerde saklanmaz, yalnızca tek
yönlü sekiz karakterlik bir özet tutulur. Kalabalık bir köken varsa konsol
uyarır.

**Kontrol.** Konsol `/admin` altında ve tek koruması yönetici anahtarıdır; bu
yüzden nginx orayı dakikada 12 isteğe sınırlar. Sabit bir adresten yönetiyorsanız
`deploy/nginx/kemi.conf` içindeki `allow`/`deny` satırlarını açın — anahtar
tahmini o zaman tamamen imkânsız hale gelir.

**Vekil sunucu.** Portal `--trust-proxy` ile çalışır ve ziyaretçinin adresini
`X-Forwarded-For` başlığının **en sağdaki** girdisinden okur; onu nginx kendisi
ekler, istemcinin uydurduğu her şey solda kalır ve yok sayılır. Bu ayar yalnızca
portal loopback'e bağlıyken doğrudur — birimler onu öyle bağlar.

**Süreçler.** Üç servis de ayrıcalıksız `kemi` kullanıcısıyla, sistemin geri
kalanı salt-okunurken (`ProtectSystem=strict`), yalnızca kendi durum dizinine
yazabilecek şekilde çalışır. Düğüm yabancıların gönderdiği işi çalıştırdığı için
Kemi zaten her görevi kaynak sınırlı bir alt süreçte koşturur; systemd bunun
arkasındaki ikinci duvardır.

**Başlıklar.** Portalın her yanıtı `nosniff`, `frame-ancestors 'none'` ve
yalnızca kendi kaynağına izin veren bir içerik güvenliği politikası taşır.

## 6. Yedekleme

Değerli olan tek şey kimlikler ve defterlerdir:

```sh
sudo systemctl stop kemi-node kemi-portal kemi-gateway
sudo tar czf kemi-yedek.tar.gz /var/lib/kemi /etc/kemi/kemi.env
sudo systemctl start kemi-node kemi-portal kemi-gateway
```

`/var/lib/kemi/node/identity.json` dosyasını kaybederseniz düğümün kimliği ve
itibarı sıfırlanır; bakiye ağdaki defterlerde o kimliğe bağlıdır.

## 7. Kaldırma

```sh
sudo systemctl disable --now kemi-node kemi-portal kemi-gateway
sudo rm /etc/systemd/system/kemi-*.service /etc/nginx/sites-enabled/kemi
sudo systemctl daemon-reload && sudo systemctl reload nginx
sudo rm -rf /opt/kemi /etc/kemi        # /var/lib/kemi'yi silmeden önce yedekleyin
sudo userdel kemi
```

---

## English summary

`deploy/install-server.sh` turns a fresh Debian/Ubuntu box into a public Kemi
deployment: an unprivileged `kemi` user, a virtualenv at `/opt/kemi/venv`,
three hardened systemd units (peer node, access portal, OpenAI gateway), an
nginx site and a Let's Encrypt certificate.

```sh
sudo ./deploy/install-server.sh --domain your.domain --tls --email you@example.com
```

Settings live in `/etc/kemi/kemi.env`; restart the services after editing.
Open `7700` for **both** tcp and udp or the node is undiscoverable.

The portal is bound to loopback and runs with `--trust-proxy`, taking the
visitor's address from the rightmost `X-Forwarded-For` entry — the one nginx
appends itself. New funded accounts are rate-limited per address
(`KEMI_GUESTS_PER_IP`), accounts from one address share a one-way *origin* tag
in the operator console, and the address itself is never stored. `/admin` is
rate-limited in nginx and can be restricted to your own IP with the
`allow`/`deny` lines in `deploy/nginx/kemi.conf`.
