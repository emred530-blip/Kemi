"""Interactive tutorial: learn Kemi by sailing a real (local) fleet.

``kemi ogren`` walks a newcomer through the whole idea in ~3 minutes with a
live in-process swarm - identity, discovery, paying for compute, streaming
AI and ranks - in plain Turkish. ``--hizli`` skips the keypresses so the
tour can run unattended (and be tested).
"""

from __future__ import annotations

import asyncio

from .consumer import Consumer, Job
from .identity import Identity
from .invite import make_invite
from .names import BANNER, rank_for, ship_name
from .node import PeerNode


def _say(text: str = "") -> None:
    print(text)


async def _pause(fast: bool) -> None:
    if fast:
        return
    await asyncio.to_thread(input, "\n  [devam etmek için Enter] ")
    print()


async def run_tutorial(fast: bool = False) -> int:
    _say(BANNER)
    _say("  Hoş geldin! Bu 5 adımlık tur, Kemi'yi çalışan gerçek bir filo")
    _say("  üzerinde öğretir. Her şey şu an bilgisayarında, yerel olarak olacak.")
    await _pause(fast)

    # ------------------------------------------------------------- 1: kimlik
    _say("ADIM 1/5 — Kimliğin: gemin")
    identity = Identity.create()
    name = ship_name(identity.node_id)
    _say(f"  Sana bir Ed25519 anahtar çifti üretildi ve ufak bir 'proof-of-work'")
    _say(f"  çözüldü (kimlik basmak bedava olmasın diye - Sybil koruması).")
    _say(f"  Gemin:    {name}")
    _say(f"  Kimliğin: {identity.node_id[:24]}… (gemi adı bundan türetilir)")
    _say(f"  Her yeni gemi kasasında 100 kredi ile denize iner.")
    await _pause(fast)

    # ------------------------------------------------------------- 2: filo
    _say("ADIM 2/5 — Filo kuruluyor (merkez yok!)")
    bootstrap = PeerNode(Identity.create(), host="127.0.0.1", port=0)
    await bootstrap.start()
    peers = [("127.0.0.1", bootstrap.port)]
    p1 = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                  provide=True, price=0.5)
    p2 = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                  provide=True, price=1.0)
    await p1.start()
    await p2.start()
    my_node = PeerNode(identity, host="127.0.0.1", port=0, bootstrap=peers)
    await my_node.start()
    consumer = Consumer(my_node)
    _say("  3 gemi denize indi ve Kademlia DHT üzerinden birbirini buldu:")
    for node, role in ((bootstrap, "sıradan eş"), (p1, "sağlayıcı, 0.5 kr/iş"),
                       (p2, "sağlayıcı, 1.0 kr/iş")):
        _say(f"    ⚓ {ship_name(node.identity.node_id):24s} {role}")
    _say("  Hiçbiri 'sunucu' değil - ilk kalkan gemi sadece ilk olandır.")
    await _pause(fast)

    # ------------------------------------------------------------- 3: keşif
    _say("ADIM 3/5 — İşlem gücü pazarı")
    found = await consumer.list_providers("hash.sha256")
    _say("  Gemin DHT'ye sordu: 'hash.sha256 koşabilen kim var?'")
    for record in found:
        _say(f"    {ship_name(record['node_id']):24s} {record['price']:.2f} kredi/iş"
             f"  [e2e şifreli{', akış' if record.get('stream') else ''}]")
    _say("  Fiyatı sağlayıcı belirler; itibarı yüksek + ucuz olan öne geçer.")
    await _pause(fast)

    # ------------------------------------------------------------- 4: iş
    _say("ADIM 4/5 — İlk yükünü taşıt (ve öde)")
    items = [f"yük-{i}" for i in range(8)]
    report = await consumer.run_job(Job(task="hash.sha256", items=items,
                                        chunk_size=2, redundancy=2))
    _say(f"  8 öğelik iş 4 parçaya bölündü, redundancy=2 ile her parça iki")
    _say(f"  AYRI gemide koşup sonuçlar çapraz doğrulandı (çoğunluk kazanır).")
    _say(f"  Ödeme: parça başına Ed25519-imzalı kredi transferi - escrow yok,")
    _say(f"  aracı yok. Harcanan: {report.spent:.2f} kredi"
         f" (hepsi uçtan uca şifreli gitti: {report.encrypted_chunks} parça).")
    balance = my_node.ledger.balance(identity.node_id)
    _say(f"  Yeni bakiyen: {balance:.2f} kredi")
    await _pause(fast)

    # ------------------------------------------------------------- 5: AI + rütbe
    _say("ADIM 5/5 — Canlı yapay zekâ akışı + rütben")
    _say("  Şimdi bir istem, tokenlar üretildikçe sana akacak:\n")
    print("    > ", end="", flush=True)
    async for event in consumer.stream_generate(["Kemi nedir?"],
                                                {"max_tokens": 10}):
        if event.get("done"):
            print()
            break
        print(event["token"], end="", flush=True)
    _say("\n  (Gerçek bir modelle aynısı için: Ollama kur, sağlayıcını")
    _say("   `--ai-backend ollama --ai-model llama3.2` ile başlat.)")
    earned_p1 = p1.ledger.total_earned(p1.identity.node_id)
    title, insignia, nxt = rank_for(earned_p1)
    _say(f"\n  Gemiler kazandıkça rütbe atlar: {ship_name(p1.identity.node_id)}")
    _say(f"  şu an {insignia} {title} ({earned_p1:.0f} kredi kazandı"
         + (f"; {title} üstü için {nxt:.0f} gerek)." if nxt else ")."))
    await _pause(fast)

    # ------------------------------------------------------------- kapanış
    invite = make_invite([("BU-MAKINENIN-IP-ADRESI", 7700)], note="örnek")
    _say("HEPSİ BU! Gerçek denize açılmak için:")
    _say("  • Filo kur:        kemi node --port 7700 --lan")
    _say("  • Arkadaş davet et: kemi davet --peer IP:7700")
    _say(f"      (kod şöyle görünür: {invite[:40]}…)")
    _say("  • Davetle katıl:   kemi katil --davet KOD")
    _say("  • Aynı Wi-Fi'daysanız kod bile gerekmez: kemi katil  (LAN keşfi)")
    _say("  • Canlı panel:     kemi node --ui 8080  →  http://127.0.0.1:8080/")

    for node in (my_node, p1, p2, bootstrap):
        await node.stop()
    return 0
