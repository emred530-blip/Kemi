"""Local end-to-end demonstration of the decentralised swarm.

Spins up, in one process and with no central component:

* a bootstrap peer (an ordinary node - it also serves as a relay),
* two honest direct providers with different prices,
* one provider "behind NAT" that relays through the bootstrap peer,
* one *dishonest* provider that returns fabricated results,
* a consumer that runs a redundancy-verified hash job and an AI job.

Then it shows that the signed payments propagated by gossip until every
replica agrees on every balance - without any tracker or ledger server.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .consumer import Consumer, Job
from .gossip_ledger import GENESIS_CREDITS
from .identity import Identity
from .node import PeerNode


class DishonestNode(PeerNode):
    """Computes nothing, returns garbage - but still takes the payment."""

    async def _execute_chunk(self, task: str, items: list[Any], params: dict) -> list[Any]:
        return ["sahte-sonuc"] * len(items)


async def _wait_for_convergence(nodes: list[PeerNode], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for node in nodes:
            await node.sync_ledger()
        counts = {node.ledger.tx_count() for node in nodes}
        if len(counts) == 1:
            return True
        await asyncio.sleep(0.5)
    return False


async def run_demo(ui_port: int | None = None) -> int:
    print("=== kemi demo: merkeziyetsiz yerel suru (tracker yok) ===\n")

    print("[1/7] bootstrap eşi başlatılıyor (sıradan bir düğüm; relay de o)...")
    bootstrap = PeerNode(Identity.create(), host="127.0.0.1", port=0)
    await bootstrap.start()
    peers = [("127.0.0.1", bootstrap.port)]
    print(f"      port {bootstrap.port} (tcp+udp), id {bootstrap.identity.short_id}")

    print("[2/7] sağlayıcılar DHT'ye katılıyor...")
    p_cheap = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                       provide=True, price=0.5)
    p_mid = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                     provide=True, price=1.0)
    p_nat = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                     provide=True, price=1.2, force_relay=True)
    p_liar = DishonestNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                           provide=True, price=0.1)
    providers = [p_cheap, p_mid, p_nat, p_liar]
    for node in providers:
        await node.start()
    await asyncio.sleep(0.3)  # let the relay session establish
    print(f"      dürüst: {p_cheap.identity.short_id} (0.5 cr), "
          f"{p_mid.identity.short_id} (1.0 cr)")
    print(f"      NAT arkasında (relay üzerinden): {p_nat.identity.short_id} (1.2 cr)")
    print(f"      HİLELİ (sahte sonuç döndürür): {p_liar.identity.short_id} (0.1 cr)")

    consumer_node = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers)
    await consumer_node.start()
    consumer = Consumer(consumer_node)
    print(f"[3/7] tüketici {consumer_node.identity.short_id} katıldı, "
          f"bakiye {await consumer.balance():.2f} kredi (genesis)")

    print("\n[4/7] hash.sha256 işi: 24 öğe, 6 parça, redundancy=2 (çapraz doğrulama)...")
    report = await consumer.run_job(Job(
        task="hash.sha256",
        items=[f"blok-{i}" for i in range(24)],
        params={"rounds": 1000},
        chunk_size=4,
        redundancy=2,
    ))
    liar_paid = consumer_node.ledger.balance(p_liar.identity.node_id) - GENESIS_CREDITS
    print(f"      {len(report.results)} sonuç doğrulandı; {report.spent:.2f} kredi harcandı")
    print(f"      tüm trafik uçtan uca şifreliydi ({report.encrypted_chunks} parça "
          f"yürütmesi; relay dahil kimse içeriği okuyamaz)")
    print(f"      hilelinin sahte sonuçlarla kazanabildiği (sınırlı) tutar: {liar_paid:.2f} kredi")
    liar_score = consumer_node.reputation.score(p_liar.identity.node_id)
    print(f"      hileli sağlayıcının tüketici gözündeki itibarı: {liar_score:.2f} "
          f"(yasaklı: {consumer_node.reputation.is_banned(p_liar.identity.node_id)})")

    print("\n[5/7] ai.generate işi sürüde çalışıyor...")
    ai_report = await consumer.run_job(Job(
        task="ai.generate",
        items=["P2P aglar neden onemli?", "Veri merkezlerinin gelecegi nedir?"],
        params={"max_tokens": 12},
        chunk_size=1,
    ))
    for prompt, completion in zip(
            ["P2P aglar neden onemli?", "Veri merkezlerinin gelecegi nedir?"],
            ai_report.results):
        print(f"      {prompt!r} -> {completion!r}")

    print("\n[6/7] pipeline paralelliği: 3 katmanlı model şeridi, katmanlar farklı")
    print("      sağlayıcılarda koşuyor (büyük modellerin bölünmesinin temeli)...")
    from .consumer import PipelineStage

    pipeline = await consumer.run_pipeline(
        [PipelineStage(task="ai.layer", params={"layer": layer}, chunk_size=2)
         for layer in range(3)],
        items=[[0.1 * i, -0.2, 0.3] for i in range(4)],
    )
    print(f"      4 gizli-durum vektörü 3 aşamadan geçti; "
          f"toplam {pipeline.spent:.2f} kredi, örnek çıktı: {pipeline.results[1]}")

    print("\n[7/7] dedikodu (gossip) yayılımı bekleniyor; replikalar karşılaştırılacak...")
    everyone = [bootstrap, *providers, consumer_node]
    converged = await _wait_for_convergence(everyone)
    print(f"      tüm {len(everyone)} replika aynı işlem kümesine yakınsadı: {converged}")

    labels = {
        consumer_node.identity.node_id: "tüketici",
        p_cheap.identity.node_id: "sağlayıcı (0.5 cr)",
        p_mid.identity.node_id: "sağlayıcı (1.0 cr)",
        p_nat.identity.node_id: "sağlayıcı (NAT/relay)",
        p_liar.identity.node_id: "sağlayıcı (hileli)",
    }
    print(f"\n      bakiyeler — iki FARKLI replikadan okunuyor "
          f"(genesis {GENESIS_CREDITS:.0f} kredi):")
    print(f"      {'hesap':24s} {'bootstrap replikası':>20s} {'tüketici replikası':>20s}")
    for node_id, label in labels.items():
        a = bootstrap.ledger.balance(node_id)
        b = consumer_node.ledger.balance(node_id)
        mark = "✓" if abs(a - b) < 1e-6 else "✗"
        print(f"      {label:24s} {a:20.2f} {b:20.2f}  {mark}")

    print("\ndemo tamamlandı: keşif DHT ile, ödeme imzalı transferlerle, doğrulama")
    print("çoğunluk oylamasıyla yapıldı; hiçbir merkezi bileşen kullanılmadı.")

    if ui_port is not None:
        from .webui import WebUI

        ui = WebUI(consumer_node, port=ui_port)
        await ui.start()
        print(f"\nsürü çalışmaya devam ediyor — canlı panel: {ui.url}")
        print("(panelden iş gönderebilirsiniz; durdurmak için Ctrl+C)")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await ui.stop()

    for node in [*providers, consumer_node, bootstrap]:
        await node.stop()
    return 0
