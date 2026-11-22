"""NF-e modelo 55 (leiaute 4.00) para qualquer empresa da conta: montar, assinar e conversar com a SEFAZ da UF.

Portado do emissor da casa (`apps/factory/src/factory_cauteloso/nfe`, que emite em produção para a Kiki Bags desde
26/09 na SVRS), sem lxml e sem erpbrasil: o XML já sai canônico (como a DPS da NFS-e), a assinatura é a de
`dps.assinar` com RSA-SHA1 (manual da NF-e 7.0) e o TLS mútuo é o de `sefin.contexto_tls` (chave só em memória).
Mantidas as regras que aquele emissor aprendeu na SEFAZ:
  - nenhum espaço ou quebra entre tags (rejeição 588, "caracteres de edição");
  - cNF nunca igual ao nNF; o destinatário em homologação tem nome fixo;
  - frete rateado pelos itens, com o último fechando a conta (regras 534/535);
  - a situação ATUAL de uma nota é o cStat do retConsSitNFe, nunca o do protNFe guardado dentro dele.

Escopo desta versão: Simples Nacional e MEI (CRT 1 e 4), ICMS por CSOSN 102/103/300/400/500, PIS/Cofins CST 49 sem
destaque. Regime normal (CRT 3) é recusado antes de ir à SEFAZ: ICMS próprio, ST e difal pedem outro desenho.
As autorizadoras das 27 UFs estão em `sefaz_nfe.json` (gerado da nfelib 3.3); só a SVRS foi medida emitindo.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import re
import secrets
import ssl
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import dps

NS = "http://www.portalfiscal.inf.br/nfe"
SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
AQUI = Path(__file__).resolve().parent
SERVIDORES = json.loads((AQUI / "sefaz_nfe.json").read_text(encoding="utf-8"))
CA_ICP = AQUI / "icp-brasil-raizes.pem"
XNOME_HOMOLOGACAO = "NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"
VER_PROC = "nota-aberta-nfe-1"
CUF = {"RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17", "MA": "21", "PI": "22",
       "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27", "SE": "28", "BA": "29", "MG": "31", "ES": "32",
       "RJ": "33", "SP": "35", "PR": "41", "SC": "42", "RS": "43", "MS": "50", "MT": "51", "GO": "52", "DF": "53"}
UF_DO_CUF = {v: k for k, v in CUF.items()}
CRT = {"simples": "1", "mei": "4"}
CSOSN = ("102", "103", "300", "400", "500")
# tPag (NT 2020.006 e 2023.004). 90 = sem pagamento (vPag 0); 99 = outros (exige xPag).
PAGAMENTOS = {"dinheiro": "01", "cheque": "02", "credito": "03", "debito": "04", "vale_alimentacao": "10",
              "boleto": "15", "deposito": "16", "pix": "17", "transferencia": "18", "sem_pagamento": "90", "outros": "99"}
SERVICOS = {"status": ("NFeStatusServico4", "nfeStatusServicoNF"), "autorizacao": ("NFeAutorizacao4", "nfeAutorizacaoLote"),
            "consulta": ("NFeConsultaProtocolo4", "nfeConsultaNF"), "evento": ("NFeRecepcaoEvento4", "nfeRecepcaoEvento")}
TEXTO_SIMPLES = "DOCUMENTO EMITIDO POR ME OU EPP OPTANTE PELO SIMPLES NACIONAL. NAO GERA DIREITO A CREDITO FISCAL DE IPI."
TEXTO_MEI = "DOCUMENTO EMITIDO POR MICROEMPREENDEDOR INDIVIDUAL (MEI). NAO GERA DIREITO A CREDITO FISCAL DE IPI."
BRASILIA = dt.timezone(dt.timedelta(hours=-3))


class NfeInvalida(ValueError):
    pass


# ------------------------------------------------------------------------------------------------- utilidades

def _dig(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def _dec(valor, campo: str) -> Decimal:
    t = str(valor if valor is not None else "").strip().replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        d = Decimal(t)
    except InvalidOperation:
        raise NfeInvalida(f"{campo}: número inválido") from None
    if not d.is_finite() or d < 0:
        raise NfeInvalida(f"{campo}: número inválido")
    return d


def d2(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.01"), ROUND_HALF_UP))


def dq(v: Decimal, casas: int) -> str:
    return str(v.quantize(Decimal(1).scaleb(-casas), ROUND_HALF_UP))


def _texto(texto, limite: int) -> str:
    """Sem espaço nas pontas nem duplo (rejeição de "caracteres de edição"), sem quebra de linha."""
    return " ".join(str(texto or "").split())[:limite]


def _el(nome: str, conteudo) -> str:
    corpo = "".join(conteudo) if isinstance(conteudo, list) else dps._esc(str(conteudo))
    return f"<{nome}>{corpo}</{nome}>"


def _opt(nome: str, conteudo) -> str:
    return _el(nome, conteudo) if conteudo not in (None, "", []) else ""


def dv_mod11(base: str) -> str:
    soma, peso = 0, 2
    for c in reversed(base):
        soma += int(c) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def cpf_valido(cpf: str) -> bool:
    c = _dig(cpf)
    if len(c) != 11 or len(set(c)) == 1:
        return False
    for n in (9, 10):
        s = sum(int(c[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(c[n]):
            return False
    return True


def cnpj_valido(cnpj: str) -> bool:
    c = _dig(cnpj)
    if len(c) != 14 or len(set(c)) == 1:
        return False
    for n in (12, 13):
        pesos = list(range(n - 7, 1, -1)) + list(range(9, 1, -1))
        s = sum(int(c[i]) * pesos[i] for i in range(n))
        dv = 0 if s % 11 < 2 else 11 - s % 11
        if dv != int(c[n]):
            return False
    return True


def chave_acesso(cuf: str, quando: dt.datetime, cnpj: str, serie: int, nnf: int, tp_emis: int, cnf: str) -> str:
    base = f"{cuf}{quando:%y%m}{_dig(cnpj):0>14}55{serie:03d}{nnf:09d}{tp_emis}{cnf}"
    if len(base) != 43:
        raise NfeInvalida("chave de acesso fora do tamanho")
    return base + dv_mod11(base)


# ------------------------------------------------------------------------------------------------- dados

@dataclass(frozen=True)
class Endereco:
    logradouro: str
    numero: str
    bairro: str
    municipio_ibge: str
    municipio: str
    uf: str
    cep: str
    complemento: str = ""
    telefone: str = ""

    def validar(self, quem: str) -> None:
        if self.uf not in CUF:
            raise NfeInvalida(f"{quem}: UF inválida")
        if len(_dig(self.municipio_ibge)) != 7 or _dig(self.municipio_ibge)[:2] != CUF[self.uf]:
            raise NfeInvalida(f"{quem}: código IBGE do município não é da UF {self.uf}")
        if len(_dig(self.cep)) != 8:
            raise NfeInvalida(f"{quem}: CEP tem 8 dígitos")
        for nome, valor in (("logradouro", self.logradouro), ("número", self.numero), ("bairro", self.bairro),
                            ("município", self.municipio)):
            if not _texto(valor, 60):
                raise NfeInvalida(f"{quem}: falta {nome}")

    def xml(self, tag: str) -> str:
        return _el(tag, [_el("xLgr", _texto(self.logradouro, 60)), _el("nro", _texto(self.numero, 60)),
                         _opt("xCpl", _texto(self.complemento, 60)), _el("xBairro", _texto(self.bairro, 60)),
                         _el("cMun", _dig(self.municipio_ibge)), _el("xMun", _texto(self.municipio, 60)),
                         _el("UF", self.uf), _el("CEP", _dig(self.cep)), _el("cPais", "1058"), _el("xPais", "BRASIL"),
                         _opt("fone", _dig(self.telefone)[:14])])


@dataclass(frozen=True)
class Emitente:
    cnpj: str
    razao: str
    ie: str
    regime: str  # simples | mei
    endereco: Endereco
    fantasia: str = ""


@dataclass(frozen=True)
class Destinatario:
    documento: str  # CPF ou CNPJ
    nome: str
    endereco: Endereco
    ie: str = ""  # vazio ou ISENTO = não contribuinte
    email: str = ""


@dataclass(frozen=True)
class Item:
    codigo: str
    descricao: str
    ncm: str
    quantidade: Decimal
    valor_unitario: Decimal
    unidade: str = "UN"
    gtin: str = ""
    cfop: str = ""  # vazio = 5102/6102 (revenda dentro/fora da UF)
    csosn: str = "102"
    origem: str = "0"
    cest: str = ""

    @property
    def total(self) -> Decimal:
        return (self.quantidade * self.valor_unitario).quantize(Decimal("0.01"), ROUND_HALF_UP)


@dataclass(frozen=True)
class Pagamento:
    forma: str  # chave de PAGAMENTOS
    valor: Decimal
    descricao: str = ""


@dataclass
class Venda:
    emitente: Emitente
    destinatario: Destinatario
    itens: list[Item]
    pagamentos: list[Pagamento]
    serie: int
    numero: int
    producao: bool
    emitida_em: dt.datetime
    frete: Decimal = Decimal(0)
    presencial: bool = False  # indPres 1 (presencial) ou 2 (internet)
    natureza: str = "VENDA DE MERCADORIA"
    informacoes: str = ""
    cnf: str = field(default_factory=lambda: f"{secrets.randbelow(100_000_000):08d}")


def montar(v: Venda) -> tuple[str, str]:
    """(chave de acesso, XML canônico sem assinatura)."""
    e, d = v.emitente, v.destinatario
    if e.regime not in CRT:
        raise NfeInvalida("NF-e por aqui é para Simples Nacional e MEI; regime normal ainda não (ICMS próprio e ST)")
    if not cnpj_valido(e.cnpj):
        raise NfeInvalida("CNPJ do emitente inválido")
    if not re.fullmatch(r"\d{2,14}", _dig(e.ie)):
        raise NfeInvalida("inscrição estadual do emitente é obrigatória na NF-e")
    e.endereco.validar("emitente")
    d.endereco.validar("destinatário")
    doc = _dig(d.documento)
    if not (cpf_valido(doc) if len(doc) == 11 else cnpj_valido(doc)):
        raise NfeInvalida("CPF ou CNPJ do destinatário inválido")
    if not _texto(d.nome, 60):
        raise NfeInvalida("nome do destinatário")
    if not v.itens or len(v.itens) > 990:
        raise NfeInvalida("a nota tem de 1 a 990 itens")
    if not (0 <= v.serie <= 999) or not (1 <= v.numero <= 999_999_999):
        raise NfeInvalida("série de 0 a 999 e número de 1 a 999.999.999")
    for n, it in enumerate(v.itens, 1):
        if not re.fullmatch(r"\d{8}", _dig(it.ncm)):
            raise NfeInvalida(f"item {n}: NCM tem 8 dígitos")
        if it.csosn not in CSOSN:
            raise NfeInvalida(f"item {n}: CSOSN aceito aqui: {', '.join(CSOSN)}")
        if it.cfop and not re.fullmatch(r"[56]\d{3}", it.cfop):
            raise NfeInvalida(f"item {n}: CFOP de saída começa com 5 (dentro da UF) ou 6 (fora)")
        if it.quantidade <= 0 or it.total <= 0:
            raise NfeInvalida(f"item {n}: quantidade e valor maiores que zero")
        if not _texto(it.descricao, 120) or not _texto(it.codigo, 60):
            raise NfeInvalida(f"item {n}: código e descrição")

    agora = v.emitida_em.astimezone(BRASILIA).replace(microsecond=0)
    cuf, uf_e, uf_d = CUF[e.endereco.uf], e.endereco.uf, d.endereco.uf
    cnf = v.cnf if v.cnf != f"{v.numero:08d}"[-8:] else f"{(int(v.cnf) + 1) % 100_000_000:08d}"
    chave = chave_acesso(cuf, agora, e.cnpj, v.serie, v.numero, 1, cnf)
    ie_dest = "" if d.ie.strip().upper() in ("", "ISENTO") else _dig(d.ie)
    contribuinte = bool(ie_dest)
    dentro = uf_e == uf_d

    total_prod = sum((i.total for i in v.itens), Decimal(0))
    frete = v.frete.quantize(Decimal("0.01"), ROUND_HALF_UP)
    fretes, acumulado = [], Decimal(0)
    for n, it in enumerate(v.itens):
        f = frete - acumulado if n == len(v.itens) - 1 else (frete * it.total / total_prod).quantize(Decimal("0.01"), ROUND_HALF_UP)
        fretes.append(f)
        acumulado += f
    vnf = total_prod + frete
    pagos = sum((p.valor for p in v.pagamentos), Decimal(0)).quantize(Decimal("0.01"))
    if not v.pagamentos:
        raise NfeInvalida("informe a forma de pagamento")
    for p in v.pagamentos:
        if p.forma not in PAGAMENTOS:
            raise NfeInvalida(f"forma de pagamento: {', '.join(PAGAMENTOS)}")
        if p.forma == "outros" and not _texto(p.descricao, 60):
            raise NfeInvalida("pagamento 'outros' pede a descrição")
    sem_pagamento = all(p.forma == "sem_pagamento" for p in v.pagamentos)
    if (pagos != 0) if sem_pagamento else (pagos != vnf.quantize(Decimal("0.01"))):
        raise NfeInvalida(f"pagamentos ({pagos}) não fecham com o total da nota ({d2(vnf)})")

    ide = [_el("cUF", cuf), _el("cNF", cnf), _el("natOp", _texto(v.natureza, 60)), _el("mod", "55"),
           _el("serie", str(v.serie)), _el("nNF", str(v.numero)), _el("dhEmi", agora.isoformat()), _el("tpNF", "1"),
           _el("idDest", "1" if dentro else "2"), _el("cMunFG", _dig(e.endereco.municipio_ibge)), _el("tpImp", "1"),
           _el("tpEmis", "1"), _el("cDV", chave[-1]), _el("tpAmb", "1" if v.producao else "2"), _el("finNFe", "1"),
           _el("indFinal", "0" if contribuinte else "1"), _el("indPres", "1" if v.presencial else "2"),
           # indIntermed só existe na venda não presencial (regra 435); aqui sempre sem intermediador.
           "" if v.presencial else _el("indIntermed", "0"), _el("procEmi", "0"), _el("verProc", VER_PROC)]
    emit = [_el("CNPJ", _dig(e.cnpj)), _el("xNome", _texto(e.razao, 60)), _opt("xFant", _texto(e.fantasia, 60)),
            e.endereco.xml("enderEmit"), _el("IE", _dig(e.ie)), _el("CRT", CRT[e.regime])]
    dest = [_el("CNPJ" if len(doc) == 14 else "CPF", doc),
            _el("xNome", XNOME_HOMOLOGACAO if not v.producao else _texto(d.nome, 60)), d.endereco.xml("enderDest"),
            _el("indIEDest", "1" if contribuinte else "9"), _opt("IE", ie_dest), _opt("email", _texto(d.email, 60))]
    dets = []
    for n, (it, f) in enumerate(zip(v.itens, fretes), 1):
        cfop = it.cfop or ("5102" if dentro else "6102")
        if (cfop[0] == "5") != dentro:
            raise NfeInvalida(f"item {n}: CFOP {cfop} não combina com destino {'na' if dentro else 'fora da'} UF")
        gtin = _dig(it.gtin) or "SEM GTIN"
        prod = [_el("cProd", _texto(it.codigo, 60)), _el("cEAN", gtin), _el("xProd", _texto(it.descricao, 120)),
                _el("NCM", _dig(it.ncm)), _opt("CEST", _dig(it.cest)), _el("CFOP", cfop), _el("uCom", _texto(it.unidade, 6)),
                _el("qCom", dq(it.quantidade, 4)), _el("vUnCom", dq(it.valor_unitario, 10)), _el("vProd", d2(it.total)),
                _el("cEANTrib", gtin), _el("uTrib", _texto(it.unidade, 6)), _el("qTrib", dq(it.quantidade, 4)),
                _el("vUnTrib", dq(it.valor_unitario, 10)), _opt("vFrete", d2(f) if f else ""), _el("indTot", "1")]
        # ICMSSN102 vale para 102, 103, 300 e 400; o 500 (ST cobrada antes) tem grupo próprio, só orig+CSOSN.
        grupo = "ICMSSN500" if it.csosn == "500" else "ICMSSN102"
        imposto = [_el("ICMS", [_el(grupo, [_el("orig", it.origem), _el("CSOSN", it.csosn)])])]
        for trib in ("PIS", "COFINS"):
            imposto.append(_el(trib, [_el(f"{trib}Outr", [_el("CST", "49"), _el("vBC", "0.00"), _el(f"p{trib}", "0.0000"),
                                                            _el(f"v{trib}", "0.00")])]))
        dets.append(f'<det nItem="{n}">' + _el("prod", prod) + _el("imposto", imposto) + "</det>")
    zero = "0.00"
    tot = [_el(t, zero) for t in ("vBC", "vICMS", "vICMSDeson", "vFCP", "vBCST", "vST", "vFCPST", "vFCPSTRet")]
    tot += [_el("vProd", d2(total_prod)), _el("vFrete", d2(frete))]
    tot += [_el(t, zero) for t in ("vSeg", "vDesc", "vII", "vIPI", "vIPIDevol", "vPIS", "vCOFINS", "vOutro")]
    tot += [_el("vNF", d2(vnf))]
    pag = [_el("detPag", [_el("indPag", "0"), _el("tPag", PAGAMENTOS[p.forma]),
                          _opt("xPag", _texto(p.descricao, 60) if p.forma == "outros" else ""),
                          _el("vPag", d2(p.valor))]) for p in v.pagamentos]
    texto = " ".join(x for x in (TEXTO_MEI if e.regime == "mei" else TEXTO_SIMPLES, _texto(v.informacoes, 4000)) if x)
    corpo = "".join([_el("ide", ide), _el("emit", emit), _el("dest", dest), *dets, _el("total", [_el("ICMSTot", tot)]),
                     _el("transp", [_el("modFrete", "0" if frete else "9")]), _el("pag", pag),
                     _el("infAdic", [_el("infCpl", texto[:5000])])])
    return chave, f'<NFe xmlns="{NS}"><infNFe Id="NFe{chave}" versao="4.00">{corpo}</infNFe></NFe>'


def assinar(xml: str, chave_privada, certificado) -> str:
    return dps.assinar(xml, chave_privada, certificado, ns=NS, sha1=True)


# ------------------------------------------------------------------------------------------------- SEFAZ

@dataclass
class Retorno:
    alcancou: bool  # False = sem resposta: a nota fica DESCONHECIDA, nunca recusada
    cstat: str = ""
    motivo: str = ""
    protocolo: str = ""
    prot_xml: str = ""
    lote_cstat: str = ""
    bruto: str = ""

    @property
    def autorizada(self) -> bool:
        return self.cstat in ("100", "150")


def autorizadora(uf: str) -> str:
    return SERVIDORES["uf_autorizadora"][uf]


def url(uf: str, producao: bool, servico: str) -> str:
    return SERVIDORES["autorizadoras"][autorizadora(uf)]["producao" if producao else "homologacao"][servico]


def envelope(servico: str, corpo: str) -> bytes:
    ns = f"http://www.portalfiscal.inf.br/nfe/wsdl/{SERVICOS[servico][0]}"
    return (f'<?xml version="1.0" encoding="utf-8"?><soap12:Envelope xmlns:soap12="{SOAP12}"><soap12:Body>'
            f'<nfeDadosMsg xmlns="{ns}">{corpo}</nfeDadosMsg></soap12:Body></soap12:Envelope>').encode()


def _resultado(resposta: bytes) -> ET.Element:
    raiz = ET.fromstring(resposta)
    for el in raiz.iter():
        if el.tag.endswith("}nfeResultMsg") and len(el):
            return el[0]
    raise ValueError("resposta SOAP sem nfeResultMsg: " + resposta[:300].decode("utf-8", "replace"))


def _t(el: ET.Element | None, tag: str) -> str:
    achado = el.find(f"{{{NS}}}{tag}") if el is not None else None
    return (achado.text or "") if achado is not None else ""


def interpretar(ret: ET.Element) -> Retorno:
    """retEnviNFe (síncrono) ou retConsSitNFe -> situação da nota."""
    prot = ret.find(f"{{{NS}}}protNFe")
    lote = _t(ret, "cStat")
    if prot is None:
        return Retorno(True, cstat=lote, motivo=_t(ret, "xMotivo"), lote_cstat=lote,
                       bruto=ET.tostring(ret, encoding="unicode"))
    inf = prot.find(f"{{{NS}}}infProt")
    ET.register_namespace("", NS)
    return Retorno(True, cstat=_t(inf, "cStat"), motivo=_t(inf, "xMotivo"), protocolo=_t(inf, "nProt"),
                   prot_xml=ET.tostring(prot, encoding="unicode", short_empty_elements=False), lote_cstat=lote,
                   bruto=ET.tostring(ret, encoding="unicode"))


def nfe_proc(nfe_assinada: str, prot_xml: str) -> str:
    """NF-e + protocolo: o XML que o emitente guarda por 5 anos e manda ao cliente."""
    return f'<?xml version="1.0" encoding="UTF-8"?><nfeProc xmlns="{NS}" versao="4.00">{nfe_assinada}{prot_xml}</nfeProc>'


def contexto(pfx: bytes, senha: str) -> ssl.SSLContext:
    """TLS mútuo com o A1 em memória (sefin.contexto_tls) confiando nas raízes públicas e na ICP-Brasil: a SVRS usa
    cadeia ICP-Brasil, a SVC-AN usa GlobalSign (medido pelo emissor da casa em 26/09)."""
    import sefin
    ctx = sefin.contexto_tls(pfx, senha)
    ctx.load_verify_locations(cafile=str(CA_ICP))
    return ctx


class Sefaz:
    def __init__(self, *, uf: str, producao: bool, pfx: bytes, senha: str, timeout: float = 60.0, transporte=None):
        if uf not in CUF:
            raise NfeInvalida("UF inválida")
        self.uf, self.producao, self.timeout = uf, producao, timeout
        self._pfx, self._senha = pfx, senha
        self._transporte = transporte or self._post

    def _post(self, endereco: str, corpo: bytes, cabecalhos: dict) -> bytes | None:
        u = urllib.parse.urlsplit(endereco)
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=self.timeout,
                                           context=contexto(self._pfx, self._senha))
        try:
            conn.request("POST", u.path + (f"?{u.query}" if u.query else ""), body=corpo, headers=cabecalhos)
            r = conn.getresponse()
            dados = r.read()
        except (OSError, http.client.HTTPException):
            return None
        finally:
            conn.close()
        if r.status >= 500 and b"nfeResultMsg" not in dados:
            return None
        return dados

    def _chamar(self, servico: str, corpo: str) -> ET.Element | None:
        ns, op = SERVICOS[servico]
        cab = {"Content-Type": f'application/soap+xml; charset=utf-8; action="http://www.portalfiscal.inf.br/nfe/wsdl/{ns}/{op}"'}
        resposta = self._transporte(url(self.uf, self.producao, servico), envelope(servico, corpo), cab)
        if resposta is None:
            return None
        try:
            return _resultado(resposta)
        except (ET.ParseError, ValueError):
            return None

    def _amb(self) -> str:
        return "1" if self.producao else "2"

    def status(self) -> Retorno:
        ret = self._chamar("status", f'<consStatServ xmlns="{NS}" versao="4.00"><tpAmb>{self._amb()}</tpAmb>'
                                     f"<cUF>{CUF[self.uf]}</cUF><xServ>STATUS</xServ></consStatServ>")
        return Retorno(False) if ret is None else Retorno(True, cstat=_t(ret, "cStat"), motivo=_t(ret, "xMotivo"))

    def autorizar(self, nfe_assinada: str, lote: str) -> Retorno:
        if f"<tpAmb>{self._amb()}</tpAmb>" not in nfe_assinada:
            raise NfeInvalida("ambiente da nota diferente do ambiente da SEFAZ chamada")
        ret = self._chamar("autorizacao", f'<enviNFe xmlns="{NS}" versao="4.00"><idLote>{lote}</idLote>'
                                          f"<indSinc>1</indSinc>{nfe_assinada}</enviNFe>")
        return Retorno(False) if ret is None else interpretar(ret)

    def consultar(self, chave: str) -> Retorno:
        ret = self._chamar("consulta", f'<consSitNFe xmlns="{NS}" versao="4.00"><tpAmb>{self._amb()}</tpAmb>'
                                       f"<xServ>CONSULTAR</xServ><chNFe>{chave}</chNFe></consSitNFe>")
        if ret is None:
            return Retorno(False)
        r = interpretar(ret)
        # A situação de agora é o cStat do retConsSitNFe; o protNFe guarda o 100 da autorização para sempre.
        return Retorno(True, cstat=_t(ret, "cStat"), motivo=_t(ret, "xMotivo"), protocolo=r.protocolo,
                       prot_xml=r.prot_xml, bruto=r.bruto)
