"""Plano de cada conta: grátis por padrão, pago se o e-mail tem assinatura com acesso no Genesis Pagar.

O Genesis Pagar (apps/genesis-pagar) já faz o checkout do Stripe e registra o estado de
cada assinatura (`acesso` só com teste ou pagamento em dia). Aqui só se pergunta a ele, por e-mail, e se escolhe o
plano de maior preço com acesso. Sem `PAGAR_TOKEN`, ou com o Pagar fora do ar, a conta fica no grátis: nunca se dá plano pago
por falha de consulta, e nunca se derruba a emissão por isso.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ARQUIVO = Path(__file__).resolve().parent / "planos.json"
PAGAR = os.environ.get("PAGAR_URL", "https://pagar.genesisinnovation.io")
CACHE_SEGUNDOS = 300


def carregar(arquivo: Path = ARQUIVO) -> list[dict]:
    return json.loads(arquivo.read_text(encoding="utf-8"))["planos"]


def sob_medida(arquivo: Path = ARQUIVO) -> dict:
    return json.loads(arquivo.read_text(encoding="utf-8")).get("sob_medida") or {}


def por_id(planos: list[dict]) -> dict[str, dict]:
    return {p["id"]: p for p in planos}


def link_checkout(plano: dict) -> str:
    return f"{PAGAR}/p/{plano['link_pagar']}" if plano.get("link_pagar") else ""


class Planos:
    def __init__(self, planos: list[dict] | None = None, consultar=None, relogio=time.monotonic):
        self.lista = planos or carregar()
        self.gratis = self.lista[0]
        self._consultar = consultar or self._consultar_pagar
        self.relogio, self._cache = relogio, {}

    def _consultar_pagar(self, email: str) -> list[dict]:
        token = os.environ.get("PAGAR_TOKEN", "")
        if not token:
            return []
        req = urllib.request.Request(f"{PAGAR}/api/assinaturas?email={urllib.parse.quote(email)}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read()).get("assinaturas", [])
        except (urllib.error.URLError, OSError, ValueError):
            return []

    def do_email(self, email: str) -> dict:
        agora = self.relogio()
        guardado = self._cache.get(email)
        if guardado and agora - guardado[0] < CACHE_SEGUNDOS:
            return guardado[1]
        links = {a.get("link") for a in self._consultar(email) if a.get("acesso")}
        pagos = [p for p in self.lista if p.get("link_pagar") and p["link_pagar"] in links]
        # Por preço, não por cota: dois planos podem ter a mesma cota de notas e diferir em empresas.
        plano = max(pagos, key=lambda p: p["preco_mes"]) if pagos else self.gratis
        self._cache[email] = (agora, plano)
        return plano

    def esquecer(self, email: str) -> None:
        """Depois de voltar do checkout: a próxima leitura pergunta de novo ao Pagar."""
        self._cache.pop(email, None)
