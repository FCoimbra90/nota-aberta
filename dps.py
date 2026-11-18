"""DPS da NFS-e nacional (a declaração que a Sefin Nacional transforma em NFS-e) e o evento de cancelamento.

Portado de `gabrielbuzziv/emissor-nfse-nacional` (MIT, src/dps.ts e container/sign.mjs, commit c3f54e8), que emite em
produção. Mantidas as regras que ele aprendeu pagando:
  - XML escrito já em forma canônica (sem espaço entre tags, sem tag autofechada, texto escapado), e por isso a
    assinatura é calculada sobre o próprio texto, sem biblioteca de C14N;
  - produção (leiaute 1.01) põe cNBS depois da descrição; a produção restrita (NT 009, esquema 1.02) põe antes e
    exige finNFSe;
  - ME/EPP informa a alíquota aproximada do Simples (pTotTribSN); MEI e não optante não podem (E0710 a E0713);
  - horário de Brasília fixo em -03:00 (sem horário de verão desde 2019).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

NS = "http://www.sped.fazenda.gov.br/nfse"
DSIG = "http://www.w3.org/2000/09/xmldsig#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
LEIAUTE = "1.01"
VER_APLIC = "nota-aberta-1"
TIPO_CNPJ = "2"
EVENTO_CANCELAMENTO = "101101"
OP_SIMPLES = {"normal": "1", "mei": "2", "simples": "3"}
BRASILIA = dt.timezone(dt.timedelta(hours=-3))


class DpsInvalida(ValueError):
    pass


@dataclass(frozen=True)
class Prestador:
    cnpj: str
    municipio_ibge: str
    regime: str  # normal | mei | simples
    inscricao_municipal: str = ""
    aliquota_simples: str = ""  # só ME/EPP, ex.: "6.00"


@dataclass(frozen=True)
class Tomador:
    documento: str  # CPF (11) ou CNPJ (14)
    nome: str
    email: str = ""
    telefone: str = ""


def _esc(texto: str) -> str:
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _el(nome: str, conteudo: str | list[str]) -> str:
    corpo = "".join(conteudo) if isinstance(conteudo, list) else _esc(conteudo)
    return f"<{nome}>{corpo}</{nome}>"


def _opt(nome: str, conteudo: str) -> str:
    return _el(nome, conteudo) if conteudo else ""


def _dig(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


def hora_brasilia(momento: dt.datetime) -> str:
    return momento.astimezone(BRASILIA).strftime("%Y-%m-%dT%H:%M:%S") + "-03:00"


def id_dps(p: Prestador, serie: str, numero: int) -> str:
    return f"DPS{p.municipio_ibge}{TIPO_CNPJ}{_dig(p.cnpj)}{serie.zfill(5)}{str(numero).zfill(15)}"


def _centavos(c: int) -> str:
    return f"{c / 100:.2f}"


def montar(*, producao: bool, prestador: Prestador, serie: str, numero: int, emitida_em: dt.datetime, competencia: str,
           codigo_servico: str, descricao: str, valor_centavos: int, tomador: Tomador | None = None, nbs: str = "") -> tuple[str, str]:
    """(id da DPS, XML canônico sem assinatura)."""
    if prestador.regime not in OP_SIMPLES:
        raise DpsInvalida("regime precisa ser normal, mei ou simples")
    if not re.fullmatch(r"\d{6}", codigo_servico or ""):
        raise DpsInvalida("código de tributação nacional (cTribNac) tem 6 dígitos, ex.: 010101")
    if nbs and not re.fullmatch(r"\d{9}", nbs):
        raise DpsInvalida("NBS tem 9 dígitos")
    if not (1 <= valor_centavos <= 99_999_999_999):
        raise DpsInvalida("valor do serviço fora da faixa")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", competencia) or competencia > hora_brasilia(emitida_em)[:10]:
        raise DpsInvalida("competência é AAAA-MM-DD e não pode ser depois da emissão")
    if not (descricao or "").strip():
        raise DpsInvalida("descreva o serviço")
    if not re.fullmatch(r"\d{1,5}", serie) or not (1 <= numero <= 999_999_999_999_999):
        raise DpsInvalida("série tem até 5 dígitos e número de 1 a 15 dígitos")
    me_epp = prestador.regime == "simples"
    desc = descricao.strip()[:2000]
    cserv = ([_el("cTribNac", codigo_servico), _el("xDescServ", desc), _opt("cNBS", nbs)] if producao
             else [_el("cTribNac", codigo_servico), _opt("cNBS", nbs), _el("xDescServ", desc)])
    tot = (_el("totTrib", [_el("pTotTribSN", prestador.aliquota_simples or "0")]) if me_epp
           else _el("totTrib", [_el("indTotTrib", "0")]))
    toma = ""
    if tomador:
        doc = _dig(tomador.documento)
        if len(doc) not in (11, 14):
            raise DpsInvalida("documento do tomador é CPF (11) ou CNPJ (14)")
        toma = _el("toma", [_el("CNPJ" if len(doc) == 14 else "CPF", doc), _el("xNome", tomador.nome.strip()[:300]),
                            _opt("fone", _dig(tomador.telefone)), _opt("email", tomador.email.strip()[:80])])
    ident = id_dps(prestador, serie, numero)
    corpo = "".join([
        _el("tpAmb", "1" if producao else "2"),
        _el("dhEmi", hora_brasilia(emitida_em)),
        _el("verAplic", VER_APLIC),
        _el("serie", serie.zfill(5)),
        _el("nDPS", str(numero)),
        _el("dCompet", competencia),
        _el("tpEmit", "1"),
        "" if producao else _el("finNFSe", "0"),
        _el("cLocEmi", prestador.municipio_ibge),
        _el("prest", [_el("CNPJ", _dig(prestador.cnpj)), _opt("IM", prestador.inscricao_municipal),
                      _el("regTrib", [_el("opSimpNac", OP_SIMPLES[prestador.regime]),
                                      _el("regApTribSN", "1") if me_epp else "", _el("regEspTrib", "0")])]),
        toma,
        _el("serv", [_el("locPrest", [_el("cLocPrestacao", prestador.municipio_ibge)]), _el("cServ", cserv)]),
        _el("valores", [_el("vServPrest", [_el("vServ", _centavos(valor_centavos))]),
                        _el("trib", [_el("tribMun", [_el("tribISSQN", "1"), _el("tpRetISSQN", "1")]), tot])]),
    ])
    return ident, (f'<?xml version="1.0" encoding="UTF-8"?><DPS xmlns="{NS}" versao="{LEIAUTE}">'
                   f'<infDPS Id="{ident}">{corpo}</infDPS></DPS>')


def cancelamento(*, producao: bool, cnpj: str, chave: str, motivo_codigo: str, motivo: str, em: dt.datetime) -> str:
    if not re.fullmatch(r"\d{50}", chave or ""):
        raise DpsInvalida("chave de acesso da NFS-e tem 50 dígitos")
    if motivo_codigo not in ("1", "2", "9"):
        raise DpsInvalida("motivo: 1 erro na emissão, 2 serviço não prestado, 9 outro")
    corpo = "".join([
        _el("tpAmb", "1" if producao else "2"), _el("verAplic", VER_APLIC), _el("dhEvento", hora_brasilia(em)),
        _el("CNPJAutor", _dig(cnpj)), _el("chNFSe", chave),
        _el(f"e{EVENTO_CANCELAMENTO}", [_el("xDesc", "Cancelamento de NFS-e"), _el("cMotivo", motivo_codigo),
                                        _el("xMotivo", motivo.strip().ljust(15, ".")[:255])]),
    ])
    return (f'<?xml version="1.0" encoding="UTF-8"?><pedRegEvento xmlns="{NS}" versao="{LEIAUTE}">'
            f'<infPedReg Id="PRE{chave}{EVENTO_CANCELAMENTO}">{corpo}</infPedReg></pedRegEvento>')


def assinar(xml: str, chave_privada, certificado) -> str:
    """XMLDSig envelopado RSA-SHA256 sobre o elemento com Id. Vale porque o XML já sai canônico: a forma C14N do
    elemento é o próprio texto com a declaração de namespace herdada (o teste confere contra o C14N do lxml)."""
    achado = re.search(r'<(inf[A-Za-z]+) Id="([^"]+)">.*</\1>', xml, re.DOTALL)
    if not achado:
        raise DpsInvalida("nenhum elemento com Id para assinar")
    elemento, nome, ident = achado.group(0), achado.group(1), achado.group(2)
    canonico = elemento.replace(f'<{nome} Id=', f'<{nome} xmlns="{NS}" Id=', 1)
    digest = base64.b64encode(hashlib.sha256(canonico.encode()).digest()).decode()
    info = (f'<CanonicalizationMethod Algorithm="{C14N}"></CanonicalizationMethod>'
            '<SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"></SignatureMethod>'
            f'<Reference URI="#{ident}"><Transforms>'
            '<Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"></Transform>'
            f'<Transform Algorithm="{C14N}"></Transform></Transforms>'
            '<DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"></DigestMethod>'
            f'<DigestValue>{digest}</DigestValue></Reference>')
    valor = base64.b64encode(chave_privada.sign(f'<SignedInfo xmlns="{DSIG}">{info}</SignedInfo>'.encode(),
                                                padding.PKCS1v15(), hashes.SHA256())).decode()
    cert_b64 = base64.b64encode(certificado.public_bytes(serialization.Encoding.DER)).decode()
    assinatura = (f'<Signature xmlns="{DSIG}"><SignedInfo>{info}</SignedInfo><SignatureValue>{valor}</SignatureValue>'
                  f'<KeyInfo><X509Data><X509Certificate>{cert_b64}</X509Certificate></X509Data></KeyInfo></Signature>')
    fecha = xml.rindex("</")
    return xml[:fecha] + assinatura + xml[fecha:]
