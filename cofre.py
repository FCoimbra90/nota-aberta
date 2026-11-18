"""Cofre do certificado A1 do cliente: cifra em envelope, nada em claro no disco.

Decisão do dono (17/11/2026): guardar o certificado cifrado, para o cliente subir uma vez só.

Desenho (envelope, o mesmo do KMS):
  - cada empresa ganha uma CHAVE DE DADOS aleatória (AES-256-GCM) que cifra o PFX e a senha;
  - a chave de dados é guardada CIFRADA pela CHAVE MESTRA, que vem do ambiente (`NOTA_CHAVE_MESTRA`, 32 bytes em
    base64, entregue pelo Secret Manager ao pod) e nunca vai para o banco, o log ou o Git;
  - o dado associado (AAD) amarra cada texto cifrado ao id da empresa e ao campo: trocar o PFX de uma empresa pelo de
    outra no banco faz a decifração falhar em vez de assinar nota com o certificado errado.
Trocar a chave mestra por um KMS é trocar `embrulhar`/`desembrulhar`, sem mexer no resto.
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSAO = b"v1"
NONCE = 12


class CofreIndisponivel(RuntimeError):
    """Sem chave mestra: o servidor recusa guardar ou abrir certificado (nunca guarda em claro)."""


class CofreViolado(RuntimeError):
    """Texto cifrado que não abre com a chave e o contexto dele: adulterado, trocado de empresa ou chave errada."""


def chave_mestra(env: dict | None = None) -> bytes:
    bruto = (env if env is not None else os.environ).get("NOTA_CHAVE_MESTRA", "")
    try:
        chave = base64.b64decode(bruto, validate=True) if bruto else b""
    except ValueError:
        chave = b""
    if len(chave) != 32:
        raise CofreIndisponivel("NOTA_CHAVE_MESTRA ausente ou fora de 32 bytes em base64: certificado não é guardado")
    return chave


def _cifrar(chave: bytes, dado: bytes, contexto: str) -> bytes:
    nonce = secrets.token_bytes(NONCE)
    return VERSAO + nonce + AESGCM(chave).encrypt(nonce, dado, contexto.encode())


def _decifrar(chave: bytes, blob: bytes, contexto: str) -> bytes:
    if not blob.startswith(VERSAO) or len(blob) < len(VERSAO) + NONCE + 16:
        raise CofreViolado("formato desconhecido")
    nonce, corpo = blob[len(VERSAO):len(VERSAO) + NONCE], blob[len(VERSAO) + NONCE:]
    try:
        return AESGCM(chave).decrypt(nonce, corpo, contexto.encode())
    except InvalidTag:
        raise CofreViolado("o texto cifrado não abre com esta chave e este contexto") from None


def embrulhar(chave_dados: bytes, empresa_id: str, mestra: bytes) -> bytes:
    return _cifrar(mestra, chave_dados, f"chave-dados:{empresa_id}")


def desembrulhar(embrulhada: bytes, empresa_id: str, mestra: bytes) -> bytes:
    return _decifrar(mestra, embrulhada, f"chave-dados:{empresa_id}")


def guardar(empresa_id: str, pfx: bytes, senha: str, mestra: bytes) -> dict[str, bytes]:
    """Devolve as três colunas a gravar. Nenhuma delas abre sem a chave mestra."""
    chave_dados = AESGCM.generate_key(bit_length=256)
    return {"chave_embrulhada": embrulhar(chave_dados, empresa_id, mestra),
            "pfx_cifrado": _cifrar(chave_dados, pfx, f"pfx:{empresa_id}"),
            "senha_cifrada": _cifrar(chave_dados, senha.encode(), f"senha:{empresa_id}")}


def abrir(empresa_id: str, registro: dict, mestra: bytes) -> tuple[bytes, str]:
    """(pfx, senha) em memória, só na hora de assinar e enviar."""
    chave_dados = desembrulhar(registro["chave_embrulhada"], empresa_id, mestra)
    pfx = _decifrar(chave_dados, registro["pfx_cifrado"], f"pfx:{empresa_id}")
    senha = _decifrar(chave_dados, registro["senha_cifrada"], f"senha:{empresa_id}").decode()
    return pfx, senha
