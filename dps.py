"""DPS da NFS-e nacional (a declaração que a Sefin Nacional transforma em NFS-e) e o evento de cancelamento.

Portado de `gabrielbuzziv/emissor-nfse-nacional` (MIT, src/dps.ts e container/sign.mjs, commit c3f54e8), que emite em
produção. Mantidas as regras que ele aprendeu pagando:
  - XML escrito já em forma canônica (sem espaço entre tags, sem tag autofechada, texto escapado), e por isso a
    assinatura é calculada sobre o próprio texto, sem biblioteca de C14N;
  - produção (leiaute 1.01) põe cNBS depois da descrição; a produção restrita (NT 009, esquema 1.02) põe antes e
    exige finNFSe;
  - ME/EPP informa a alíquota aproximada do Simples (pTotTribSN); MEI e não optante não podem (E0710 a E0713);
  - horário de Brasília fixo em -03:00 (sem horário de verão desde 2019);
  - grupo IBSCBS (Reforma Tributária) em dois formatos, medidos na Sefin em 20/11/2026: a produção ainda valida o
    esquema anterior (finNFSe dentro do grupo e CST/cClassTrib dentro de gIBSCBS); a produção restrita, o 1.02 (sem
    finNFSe no grupo e CST/cClassTrib direto em trib). Quem calcula CBS e IBS na nota é a Sefin; a DPS só classifica.
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
REG_ESP_TRIB = ("0", "1", "2", "3", "4", "5", "6", "9")  # enumeração do TSRegEspTrib, XSD 1.01
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
    regime_especial: str = "0"  # TSRegEspTrib: 0 nenhum, 1 cooperativa, 2 estimativa, 3 ME municipal, 4 notário, 5 autônomo, 6 sociedade de profissionais, 9 outros


@dataclass(frozen=True)
class Tomador:
    documento: str  # CPF (11) ou CNPJ (14)
    nome: str
    email: str = ""
    telefone: str = ""
    # Endereço nacional (TCEndereco/endNac do XSD 1.01): vai inteiro ou não vai. Pedido do Beto (contador, 19/11).
    cep: str = ""
    municipio_ibge: str = ""
    logradouro: str = ""
    numero: str = ""
    complemento: str = ""
    bairro: str = ""


@dataclass(frozen=True)
class IbsCbs:
    """Classificação da operação na Reforma Tributária. O emissor preenche pelo NBS com o motor oficial da Receita."""
    cst: str  # 3 dígitos, ex.: 000
    cclass_trib: str  # 6 dígitos, ex.: 000001
    cind_op: str  # código indicador da operação, 6 dígitos, ex.: 100301
    ind_final: str = "0"  # 1 = uso ou consumo pessoal (art. 57)
    ind_dest: str = "0"  # 0 = o destinatário é o próprio tomador


@dataclass(frozen=True)
class Retencoes:
    """Retenções na fonte (Lucro Real/Presumido, órgão público, empresa que retém). Valores em centavos."""
    iss_retido: bool = False
    aliquota_iss: str = ""  # "2.00"; obrigatória com ISS retido de ME/EPP (E0621), mínimo 1,8%
    irrf: int = 0
    csll: int = 0
    inss: int = 0  # contribuição previdenciária retida (vRetCP)
    pis: int = 0
    cofins: int = 0
    pis_cofins_retidos: bool = False


def _ibscbs(g: IbsCbs | None, producao: bool) -> str:
    if g is None:
        return ""
    for nome, valor, padrao in (("CST", g.cst, r"\d{3}"), ("cClassTrib", g.cclass_trib, r"\d{6}"),
                                ("indicador da operação (cIndOp)", g.cind_op, r"\d{6}"),
                                ("indFinal", g.ind_final, r"[01]"), ("indDest", g.ind_dest, r"[01]")):
        if not re.fullmatch(padrao, valor or ""):
            raise DpsInvalida(f"IBS/CBS: {nome} inválido")
    classe = [_el("CST", g.cst), _el("cClassTrib", g.cclass_trib)]
    trib = _el("trib", [_el("gIBSCBS", classe)] if producao else classe)
    return _el("IBSCBS", [_el("finNFSe", "0") if producao else "", _el("indFinal", g.ind_final), _el("cIndOp", g.cind_op),
                          _el("indDest", g.ind_dest), _el("valores", [trib])])


def _tributos(r: Retencoes, prestador: Prestador, valor_centavos: int) -> tuple[str, str]:
    """(tribMun, tribFed)."""
    aliq = (r.aliquota_iss or "").replace(",", ".").strip()
    if aliq:
        if not re.fullmatch(r"\d{1,2}(\.\d{1,2})?", aliq) or not (0 < float(aliq) <= 5):
            raise DpsInvalida("alíquota do ISS em %, até 5, ex.: 2.00")
        aliq = f"{float(aliq):.2f}"
    if r.iss_retido and prestador.regime == "simples" and not aliq:
        raise DpsInvalida("ISS retido de ME/EPP do Simples exige a alíquota do ISS (mínimo 1,8%)")
    valores = (r.irrf, r.csll, r.inss, r.pis, r.cofins)
    if any(v < 0 for v in valores):
        raise DpsInvalida("retenção não pode ser negativa")
    iss = round(valor_centavos * float(aliq) / 100) if (r.iss_retido and aliq) else 0
    if sum(valores) + iss >= valor_centavos:
        raise DpsInvalida("as retenções somam o valor do serviço inteiro ou mais")
    mun = _el("tribMun", [_el("tribISSQN", "1"), _el("tpRetISSQN", "2" if r.iss_retido else "1"), _opt("pAliq", aliq)])
    if not any(valores):
        return mun, ""
    piscofins = ""
    if r.pis or r.cofins:
        if r.pis_cofins_retidos:
            tipo = "3" if r.csll else "4"
        else:
            tipo = "8" if r.csll else "2"
        piscofins = _el("piscofins", [_el("CST", "01"), _el("vBCPisCofins", _centavos(valor_centavos)),
                                      _opt("vPis", _centavos(r.pis) if r.pis else ""),
                                      _opt("vCofins", _centavos(r.cofins) if r.cofins else ""), _el("tpRetPisCofins", tipo)])
    fed = _el("tribFed", [piscofins, _opt("vRetCP", _centavos(r.inss) if r.inss else ""),
                          _opt("vRetIRRF", _centavos(r.irrf) if r.irrf else ""),
                          _opt("vRetCSLL", _centavos(r.csll) if r.csll else "")])
    return mun, fed


def _endereco(t: Tomador) -> str:
    campos = (t.cep, t.municipio_ibge, t.logradouro, t.numero, t.complemento, t.bairro)
    if not any((c or "").strip() for c in campos):
        return ""
    cep, mun = _dig(t.cep), _dig(t.municipio_ibge)
    if len(cep) != 8:
        raise DpsInvalida("CEP do tomador tem 8 dígitos")
    if len(mun) != 7:
        raise DpsInvalida("município do tomador é o código IBGE de 7 dígitos (o CEP preenche no painel)")
    if not (t.logradouro or "").strip() or not (t.numero or "").strip() or not (t.bairro or "").strip():
        raise DpsInvalida("endereço do tomador precisa de logradouro, número e bairro (use S/N sem número)")
    return _el("end", [_el("endNac", [_el("cMun", mun), _el("CEP", cep)]), _el("xLgr", t.logradouro.strip()[:255]),
                       _el("nro", t.numero.strip()[:60]), _opt("xCpl", (t.complemento or "").strip()[:156]),
                       _el("xBairro", t.bairro.strip()[:60])])


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
           codigo_servico: str, descricao: str, valor_centavos: int, tomador: Tomador | None = None, nbs: str = "",
           ibscbs: IbsCbs | None = None, retencoes: Retencoes | None = None) -> tuple[str, str]:
    """(id da DPS, XML canônico sem assinatura)."""
    if prestador.regime not in OP_SIMPLES:
        raise DpsInvalida("regime precisa ser normal, mei ou simples")
    if prestador.aliquota_simples and prestador.regime != "simples":
        raise DpsInvalida("alíquota do Simples só vale para ME/EPP optante; MEI e regime normal não informam (E0710 a E0713)")
    if prestador.aliquota_simples and not re.fullmatch(r"\d{1,2}\.\d{2}", prestador.aliquota_simples):
        raise DpsInvalida("alíquota do Simples em %, ex.: 6.00")
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
    if prestador.regime_especial not in REG_ESP_TRIB:
        raise DpsInvalida("regime especial de tributação fora do leiaute (0 a 6 ou 9)")
    if prestador.inscricao_municipal and not re.fullmatch(r"[A-Za-z0-9]{1,15}", prestador.inscricao_municipal):
        raise DpsInvalida("inscrição municipal tem de 1 a 15 letras ou dígitos")
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
                            _endereco(tomador), _opt("fone", _dig(tomador.telefone)), _opt("email", tomador.email.strip()[:80])])
    trib_mun, trib_fed = _tributos(retencoes or Retencoes(), prestador, valor_centavos)
    grupo_rtc = _ibscbs(ibscbs, producao)
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
                                      _el("regApTribSN", "1") if me_epp else "", _el("regEspTrib", prestador.regime_especial)])]),
        toma,
        _el("serv", [_el("locPrest", [_el("cLocPrestacao", prestador.municipio_ibge)]), _el("cServ", cserv)]),
        _el("valores", [_el("vServPrest", [_el("vServ", _centavos(valor_centavos))]),
                        _el("trib", [trib_mun, trib_fed, tot])]),
        grupo_rtc,
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


def assinar(xml: str, chave_privada, certificado, *, ns: str = NS, sha1: bool = False) -> str:
    """XMLDSig envelopado sobre o elemento com Id. Vale porque o XML já sai canônico: a forma C14N do elemento é o
    próprio texto com a declaração de namespace herdada (o teste confere contra o C14N do lxml). A NF-e (manual 7.0)
    exige RSA-SHA1 e digest SHA1 (`sha1=True`, `ns` do portal fiscal); a NFS-e nacional usa SHA-256."""
    achado = re.search(r'<(inf[A-Za-z]+) Id="([^"]+)"[^>]*>.*</\1>', xml, re.DOTALL)
    if not achado:
        raise DpsInvalida("nenhum elemento com Id para assinar")
    elemento, nome, ident = achado.group(0), achado.group(1), achado.group(2)
    canonico = elemento.replace(f'<{nome} Id=', f'<{nome} xmlns="{ns}" Id=', 1)
    resumo, metodo, alg = ((hashlib.sha1, "http://www.w3.org/2000/09/xmldsig#rsa-sha1", "http://www.w3.org/2000/09/xmldsig#sha1")
                           if sha1 else (hashlib.sha256, "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
                                         "http://www.w3.org/2001/04/xmlenc#sha256"))
    digest = base64.b64encode(resumo(canonico.encode()).digest()).decode()
    info = (f'<CanonicalizationMethod Algorithm="{C14N}"></CanonicalizationMethod>'
            f'<SignatureMethod Algorithm="{metodo}"></SignatureMethod>'
            f'<Reference URI="#{ident}"><Transforms>'
            '<Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"></Transform>'
            f'<Transform Algorithm="{C14N}"></Transform></Transforms>'
            f'<DigestMethod Algorithm="{alg}"></DigestMethod>'
            f'<DigestValue>{digest}</DigestValue></Reference>')
    valor = base64.b64encode(chave_privada.sign(f'<SignedInfo xmlns="{DSIG}">{info}</SignedInfo>'.encode(),
                                                padding.PKCS1v15(), hashes.SHA1() if sha1 else hashes.SHA256())).decode()
    cert_b64 = base64.b64encode(certificado.public_bytes(serialization.Encoding.DER)).decode()
    assinatura = (f'<Signature xmlns="{DSIG}"><SignedInfo>{info}</SignedInfo><SignatureValue>{valor}</SignatureValue>'
                  f'<KeyInfo><X509Data><X509Certificate>{cert_b64}</X509Certificate></X509Data></KeyInfo></Signature>')
    fecha = xml.rindex("</")
    return xml[:fecha] + assinatura + xml[fecha:]
