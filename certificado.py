"""Lê e confere o certificado A1 (PKCS#12) que o cliente sobe, antes de guardar.

O que se recusa na porta, para nada falhar só na hora de emitir:
  - arquivo que não abre com a senha (senha errada ou arquivo que não é PFX);
  - certificado vencido ou que ainda não vale;
  - certificado de outro CNPJ (a nota sai em nome da empresa cadastrada, e assinar com o e-CNPJ de outra seria fraude);
  - PFX sem chave privada (não assina nada).
O CNPJ de um e-CNPJ ICP-Brasil vem no SAN (otherName 2.16.76.1.3.3) e, por costume, no fim do CN ("RAZAO:CNPJ").
"""
from __future__ import annotations

import datetime as dt
import re

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

OID_CNPJ = x509.ObjectIdentifier("2.16.76.1.3.3")
TAMANHO_MAX = 64 * 1024  # um A1 tem poucos KB; acima disso não é certificado


class CertificadoRecusado(ValueError):
    pass


def so_digitos(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _cnpj_do_san(cert: x509.Certificate) -> str | None:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    for nome in san.get_values_for_type(x509.OtherName):
        if nome.type_id == OID_CNPJ:
            achado = re.search(rb"\d{14}", nome.value)
            if achado:
                return achado.group().decode()
    return None


def _cnpj_do_cn(cert: x509.Certificate) -> str | None:
    for atributo in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        achado = re.search(r":(\d{14})\s*$", str(atributo.value))
        if achado:
            return achado.group(1)
    return None


def ler(pfx: bytes, senha: str, *, cnpj_esperado: str, agora: dt.datetime | None = None) -> dict:
    if not pfx or len(pfx) > TAMANHO_MAX:
        raise CertificadoRecusado("arquivo vazio ou grande demais para ser um certificado A1")
    try:
        chave, cert, _ = pkcs12.load_key_and_certificates(pfx, senha.encode() if senha else None)
    except (ValueError, TypeError):
        raise CertificadoRecusado("o arquivo não abriu com essa senha (senha errada ou não é um .pfx/.p12)") from None
    if chave is None or cert is None:
        raise CertificadoRecusado("o arquivo não tem a chave privada do certificado")
    agora = agora or dt.datetime.now(dt.timezone.utc)
    inicio, fim = cert.not_valid_before_utc, cert.not_valid_after_utc
    if agora < inicio:
        raise CertificadoRecusado(f"o certificado só passa a valer em {inicio:%d/%m/%Y}")
    if agora > fim:
        raise CertificadoRecusado(f"o certificado venceu em {fim:%d/%m/%Y}")
    cnpj = _cnpj_do_san(cert) or _cnpj_do_cn(cert)
    esperado = so_digitos(cnpj_esperado)
    if not cnpj:
        raise CertificadoRecusado("não achei o CNPJ dentro do certificado: ele precisa ser um e-CNPJ A1")
    if cnpj != esperado:
        raise CertificadoRecusado(f"o certificado é do CNPJ {cnpj}, não do {esperado} cadastrado")
    titular = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return {"cnpj": cnpj, "titular": str(titular[0].value) if titular else "", "valido_ate": fim.date().isoformat(),
            "dias_restantes": (fim - agora).days}
