"""Conversa com a Sefin Nacional (NFS-e) por TLS mútuo com o A1 do emitente, sem a chave privada tocar o disco.

O `NfseClient` da nfelib 3.3 grava a chave privada em PEM sem senha num arquivo temporário com `delete=False` (nunca
apagado) e manda a DPS para `adn.../contribuinte`. Aqui: a chave vai para um arquivo que só existe na memória
(`memfd_create`, Linux), o `ssl` lê de lá e o descritor é fechado na hora; e o endereço é o da Sefin Nacional, que é o
que a página oficial (gov.br, "APIs - Prod. Restrita e Produção") publica e o que o emissor-nfse-nacional usa em
produção (`POST /SefinNacional/nfse` com `dpsXmlGZipB64`).
"""
from __future__ import annotations

import base64
import gzip
import http.client
import json
import os
import ssl
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

HOSTS = {True: "sefin.nfse.gov.br", False: "sefin.producaorestrita.nfse.gov.br"}
PREFIXO = "/SefinNacional"
DPS_DUPLICADA = "E0014"


@dataclass
class Resposta:
    alcancou: bool  # False = não houve resposta do governo (rede, tempo): o resultado é DESCONHECIDO
    status: int = 0
    dados: dict = field(default_factory=dict)

    def erros(self) -> list[dict]:
        bruto = self.dados.get("erros") or self.dados.get("Erros") or self.dados.get("erro") or []
        lista = bruto if isinstance(bruto, list) else [bruto]
        saida = [{"codigo": str(e.get("Codigo") or e.get("codigo") or ""),
                  "mensagem": str(e.get("Descricao") or e.get("descricao") or e.get("Mensagem") or ""),
                  **({"detalhe": str(e["Complemento"])} if e.get("Complemento") else {})}
                 for e in lista if isinstance(e, dict)]
        if saida or self.status < 400:
            return saida
        return [{"codigo": f"HTTP {self.status}", "mensagem": json.dumps(self.dados, ensure_ascii=False)[:500]}]


def decodificar(corpo: bytes) -> dict:
    """JSON da Sefin; cada campo `...XmlGZipB64` ganha a versão aberta sem o sufixo (`nfseXml`, por exemplo)."""
    try:
        dados = json.loads(corpo.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"bruto": corpo[:4000].decode("utf-8", "replace")}
    if not isinstance(dados, dict):
        return {"bruto": str(dados)[:4000]}
    for chave, valor in list(dados.items()):
        if chave.endswith("XmlGZipB64") and isinstance(valor, str):
            try:
                dados[chave.replace("GZipB64", "")] = gzip.decompress(base64.b64decode(valor)).decode("utf-8")
            except (ValueError, OSError):
                pass
    return dados


def contexto_tls(pfx: bytes, senha: str) -> ssl.SSLContext:
    chave, cert, cadeia = pkcs12.load_key_and_certificates(pfx, senha.encode())
    pem = chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pem += cert.public_bytes(serialization.Encoding.PEM)
    for extra in cadeia or []:
        pem += extra.public_bytes(serialization.Encoding.PEM)
    ctx = ssl.create_default_context()
    fd = os.memfd_create("a1", os.MFD_CLOEXEC)  # só memória: some quando o descritor fecha
    try:
        os.write(fd, pem)
        ctx.load_cert_chain(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)
    return ctx


class Sefin:
    def __init__(self, *, producao: bool, pfx: bytes, senha: str, timeout: float = 40.0, host: str | None = None,
                 contexto: ssl.SSLContext | None = None, porta: int = 443):
        self.host = host or HOSTS[producao]
        self.porta, self.timeout = porta, timeout
        self.ctx = contexto or contexto_tls(pfx, senha)

    def _pedir(self, metodo: str, caminho: str, corpo: dict | None = None) -> Resposta:
        conexao = http.client.HTTPSConnection(self.host, self.porta, timeout=self.timeout, context=self.ctx)
        try:
            dados = json.dumps(corpo).encode() if corpo is not None else None
            conexao.request(metodo, PREFIXO + caminho, body=dados,
                            headers={"Content-Type": "application/json", "Accept": "application/json"})
            r = conexao.getresponse()
            return Resposta(True, r.status, decodificar(r.read()))
        except (OSError, http.client.HTTPException):
            return Resposta(False)
        finally:
            conexao.close()

    def enviar_dps(self, dps_assinada: str) -> Resposta:
        comprimido = base64.b64encode(gzip.compress(dps_assinada.encode("utf-8"))).decode("ascii")
        return self._pedir("POST", "/nfse", {"dpsXmlGZipB64": comprimido})

    def consultar_dps(self, id_dps: str) -> Resposta:
        return self._pedir("GET", f"/dps/{id_dps}")

    def consultar_nfse(self, chave: str) -> Resposta:
        return self._pedir("GET", f"/nfse/{chave}")

    def consultar_evento(self, chave: str, tipo: str, sequencia: int = 1) -> Resposta:
        """200 = o evento está registrado no governo (medido em 18/11 com o cancelamento 101101/1)."""
        return self._pedir("GET", f"/nfse/{chave}/eventos/{tipo}/{sequencia}")

    def enviar_evento(self, chave: str, evento_assinado: str) -> Resposta:
        comprimido = base64.b64encode(gzip.compress(evento_assinado.encode("utf-8"))).decode("ascii")
        return self._pedir("POST", f"/nfse/{chave}/eventos", {"pedidoRegistroEventoXmlGZipB64": comprimido})
