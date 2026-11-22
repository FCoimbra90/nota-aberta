"""Emitir NFS-e nacional por empresa: numeração, cota, assinatura, envio e as salvaguardas contra nota dupla.

Salvaguardas (do emissor-nfse-nacional, que as aprendeu em produção):
  - pedido repetido é seguro: quem chama dá um `ref`; o mesmo ref devolve a mesma nota, nunca uma segunda;
  - resposta perdida (rede, tempo, 5xx) deixa a nota DESCONHECIDA, nunca recusada; na próxima tentativa consulta-se a
    DPS na Sefin antes de reenviar, e só se ela não existir manda de novo;
  - E0014 (número de DPS já usado) consulta: se a nota é nossa (mesmo valor e tomador) vira emitida; se não, pula o
    número, no máximo 5 vezes;
  - dhEmi um minuto atrás do nosso relógio, para não ficar à frente do relógio do governo;
  - nota recusada devolve a cota do mês; desconhecida não devolve (pode ter saído).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets

from cryptography.hazmat.primitives.serialization import pkcs12

import dps
import rtc_nfse
from contas import Contas, Erro, _agora, mes_atual
from sefin import DPS_DUPLICADA, Sefin

ESQUEMA = """
create table if not exists numeracao (empresa_id text primary key references empresas(id) on delete cascade, ultimo integer not null);
create table if not exists notas (
  id text primary key, empresa_id text not null references empresas(id) on delete cascade, ref text not null,
  status text not null, pedido text not null, numero integer not null, id_dps text not null default '',
  chave text not null default '', nfse_xml text not null default '', erros text not null default '[]',
  producao integer not null, mes text not null, criada text not null, atualizada text not null,
  unique (empresa_id, ref));
"""
SERIE = "1"
MAX_COLISOES = 5
ATRASO_RELOGIO = dt.timedelta(seconds=60)
ENDERECO = ("cep", "municipio_ibge", "logradouro", "numero", "complemento", "bairro")
RETIDOS = ("irrf", "csll", "inss", "pis", "cofins")

# O texto do governo diz o que falhou, não o que fazer. Medido em 18/11/2026 na produção restrita: Ipu/CE ativo em
# produção e inativo no teste (E0037); Rio/RJ ativo no teste mas sem nenhuma alíquota cadastrada (E0312 em todo código).
DICAS = {
    "E0037": {False: "O seu município ainda não está ativo no ambiente de TESTE do governo (produção restrita). Isso não "
                     "diz nada da produção: lá ele pode estar ativo. Fale com a gente para emitir em produção.",
              True: "O seu município ainda não aderiu ao sistema nacional da NFS-e. Enquanto isso, a nota sai pelo sistema "
                    "da prefeitura."},
    "E0312": {False: "O município não tem este código de serviço cadastrado no ambiente de TESTE do governo. Muitas "
                     "prefeituras não cadastram as alíquotas no teste; em produção o cadastro costuma estar completo.",
              True: "O seu município não usa este código de serviço na data da competência. Confira o código da lista "
                    "nacional com o seu contador."},
}


DICAS["E0234"] = {k: "Preencha o endereço do tomador (abra \"Endereço do tomador\" no formulário: CEP, número e bairro). "
                      "Para este tipo de serviço o governo exige o endereço de quem recebe a nota." for k in (False, True)}

# E0116/E0120 (22/11, Rony em SP e uma empresa de BH): a prefeitura cadastra no CNC nacional se a IM é obrigatória e
# qual é. A Nota Aberta manda a IM da empresa em <prest><IM> (conferido montando a DPS no pod); o governo compara com o
# cadastro dele, dígito a dígito. Relatos de outros emissores: no ambiente de TESTE o cadastro costuma faltar ou divergir.
DICAS["E0116"] = {
    False: ("A prefeitura exige a inscrição municipal e o governo não reconheceu a que foi enviada. Confira em \"Dados "
            "fiscais\" da empresa: só os números, exatamente como na prefeitura (com os zeros do começo, se houver). "
            "Se estiver certa, é o ambiente de TESTE do governo sem o seu cadastro: emita com valor fiscal e, se "
            "quiser, cancele em seguida."),
    True: ("A prefeitura exige a inscrição municipal e o governo não reconheceu a que foi enviada. Confira em \"Dados "
           "fiscais\" da empresa: só os números, exatamente como no cadastro da prefeitura (com os zeros do começo, se "
           "houver). Se persistir, a prefeitura precisa conferir o seu cadastro no sistema nacional (CNC)."),
}
DICAS["E0120"] = {k: ("Esta prefeitura não usa inscrição municipal na nota nacional. Apague a inscrição em \"Dados "
                      "fiscais\" da empresa e emita de novo.") for k in (False, True)}

# Pedido do Thiago (21/11): "quando dá erro, a pessoa sabe exatamente o motivo". O Rodrigo (X, 20/11) recebeu a página
# HTML inteira de um 403 do ambiente de teste no lugar de uma frase. Toda falha vira: o que aconteceu (mensagem limpa)
# + o que fazer (dica). Vale para nota nova e para a que já está no banco, porque é aplicado na leitura (`publica`).
DICA_ENTRADA = "Nada foi enviado ao governo e a nota não contou na cota: corrija no formulário e emita de novo."
DICA_SEM_CODIGO = ("Este é o texto do governo. Se não ficar claro o que mudar, use o botão Repetir, ajuste e tente de novo, "
                   "ou fale com o suporte.")


def _texto(mensagem: str) -> str:
    """Tira HTML e o embrulho {"bruto": ...} que algumas respostas de erro trazem."""
    t = str(mensagem or "")
    try:
        bruto = json.loads(t)
        if isinstance(bruto, dict) and "bruto" in bruto:
            t = str(bruto["bruto"])
    except ValueError:
        pass
    t = re.sub(r"<(style|script)[^>]*>.*?</\1>|<!--.*?-->", " ", t, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|\\[rn]", " ", t)).strip()


def _http(status: int, producao: bool) -> tuple[str, str]:
    if status in (401, 403):
        if producao:
            return ("O governo recusou a conexão com o certificado desta empresa.",
                    ("Confira no painel se o certificado é o e-CNPJ A1 da própria empresa e está dentro da validade. "
                     "Se estiver tudo certo, tente de novo em alguns minutos: nada foi emitido nem contou na cota."))
        return ("O ambiente de TESTE do governo recusou a conexão.",
                ("É instabilidade do lado do governo (em 20 e 21/11 o teste ficou horas fora do ar), não do seu cadastro. "
                 "Tente mais tarde ou emita direto com valor fiscal: nada foi emitido nem contou na cota."))
    if status >= 500:
        return ("O sistema do governo está com problema agora.", "Tente de novo em alguns minutos: nada foi emitido.")
    return (f"O governo devolveu um erro sem explicação (HTTP {status}).", DICA_SEM_CODIGO)


def explicar(e: dict, producao: bool) -> dict:
    codigo = str(e.get("codigo") or "")
    if codigo.startswith("HTTP "):
        try:
            mensagem, dica = _http(int(codigo[5:]), producao)
        except ValueError:
            mensagem, dica = _texto(e.get("mensagem")), DICA_SEM_CODIGO
        return e | {"mensagem": mensagem, "dica": dica}
    if codigo in DICAS:
        return e | {"mensagem": _texto(e.get("mensagem")), "dica": DICAS[codigo][producao]}
    if codigo == "entrada":
        return e | {"dica": e.get("dica") or DICA_ENTRADA}
    return e | {"mensagem": _texto(e.get("mensagem")), "dica": e.get("dica") or DICA_SEM_CODIGO}


def com_dicas(erros: list[dict], producao: bool) -> list[dict]:
    return [explicar(e, producao) for e in erros]


def _valor_xml(xml: str, tag: str) -> str:
    achado = re.search(f"<{tag}>([^<]*)</{tag}>", xml or "")
    return achado.group(1) if achado else ""


def tributos_da_nota(nfse_xml: str) -> dict:
    """O que a Sefin calculou na NFS-e (não o que pedimos): ISS, retenções, líquido e o grupo IBS/CBS da Reforma.
    Lê só a parte da NFS-e, antes da DPS que vai embutida nela."""
    xml = (nfse_xml or "").split("<DPS", 1)[0]
    if not xml:
        return {}
    rtc = xml.split("<IBSCBS>", 1)[1] if "<IBSCBS>" in xml else ""
    iss = xml.split("<IBSCBS>", 1)[0]
    campos = {"iss": _valor_xml(iss, "vISSQN"), "aliquota_iss": _valor_xml(iss, "pAliqAplic"),
              "retencoes": _valor_xml(iss, "vTotalRet"), "liquido": _valor_xml(iss, "vLiq"),
              "base_ibs_cbs": _valor_xml(rtc, "vBC"), "cbs": _valor_xml(rtc, "vCBS"), "ibs": _valor_xml(rtc, "vIBSTot"),
              "aliquota_cbs": _valor_xml(rtc, "pCBS"), "aliquota_ibs_uf": _valor_xml(rtc, "pIBSUF"),
              "aliquota_ibs_mun": _valor_xml(rtc, "pIBSMun")}
    return {k: v for k, v in campos.items() if v}


def numero_br(valor) -> float:
    """"1.500,00" e "1500,00" (como se escreve no Brasil) e "1500.00" valem o mesmo. Antes, "1.000,00" virava erro."""
    t = str(valor if valor is not None else "").strip().replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    return float(t)


# Maior alíquota nominal do Simples é 33% (Anexo IV, 6ª faixa): acima disso é digitação errada, não tributo.
ALIQUOTA_SIMPLES_MAX = 33.0


def _aliquota_simples(valor) -> str:
    """% aproximado de tributos do Simples na nota (pTotTribSN), como no emissor oficial. Vazio = 0 (o padrão de antes)."""
    if valor in (None, ""):
        return ""
    try:
        v = numero_br(valor)
    except ValueError:
        raise Erro(422, "aliquota_simples_invalida", "alíquota do Simples em %, ex.: 6,00") from None
    if not 0 < v <= ALIQUOTA_SIMPLES_MAX:
        raise Erro(422, "aliquota_simples_invalida", f"alíquota do Simples fica entre 0 e {ALIQUOTA_SIMPLES_MAX:.0f}%")
    return f"{v:.2f}"


def _reais(valor, campo: str) -> int:
    try:
        centavos = round(numero_br(valor or 0) * 100)
    except ValueError:
        raise Erro(422, "retencao_invalida", f"{campo} em reais, ex.: 15.00") from None
    if centavos < 0:
        raise Erro(422, "retencao_invalida", f"{campo} não pode ser negativo")
    return centavos


class Emissor:
    def __init__(self, contas: Contas, *, producao: bool, fabrica_sefin=Sefin, agora=None, classificar=None):
        # `producao` = o servidor PERMITE nota com valor fiscal; quem escolhe é cada pedido (`ambiente`, padrão "teste").
        self.c, self.producao, self.fabrica_sefin = contas, producao, fabrica_sefin
        self.agora = agora or (lambda: dt.datetime.now(dt.timezone.utc))
        # (nbs, data, escolha) -> {cst, cclass_trib, cind_op}: o motor oficial da Receita (rtc_nfse) classifica.
        self.classificar = classificar or rtc_nfse.classificar
        self.c.bd.executescript(ESQUEMA)

    # ------------------------------------------------------------- registro

    def _proximo_numero(self, empresa_id: str) -> int:
        with self.c.trava:
            self.c.bd.execute("insert or ignore into numeracao values (?, 0)", (empresa_id,))
            self.c.bd.execute("update numeracao set ultimo = ultimo + 1 where empresa_id = ?", (empresa_id,))
            return self.c.bd.execute("select ultimo from numeracao where empresa_id = ?", (empresa_id,)).fetchone()[0]

    def _salvar(self, nota_id: str, **campos) -> dict:
        campos["atualizada"] = _agora()
        if "erros" in campos:
            campos["erros"] = json.dumps(campos["erros"], ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in campos)
        with self.c.trava:
            self.c.bd.execute(f"update notas set {sets} where id = ?", (*campos.values(), nota_id))
        return self.nota_por_id(nota_id)

    def nota_por_id(self, nota_id: str) -> dict:
        n = dict(self.c.bd.execute("select * from notas where id = ?", (nota_id,)).fetchone())
        n["pedido"], n["erros"] = json.loads(n["pedido"]), json.loads(n["erros"])
        return n

    def notas(self, usuario_id: str, empresa_id: str, limite: int = 50) -> list[dict]:
        self.c.empresa(usuario_id, empresa_id)
        ids = self.c.bd.execute("select id from notas where empresa_id = ? order by criada desc limit ?",
                                (empresa_id, limite)).fetchall()
        return [self.publica(self.nota_por_id(i[0])) for i in ids]

    @staticmethod
    def publica(n: dict) -> dict:
        return {k: n[k] for k in ("id", "ref", "status", "numero", "chave", "criada", "atualizada")} | {
            "erros": com_dicas(n["erros"], bool(n["producao"])),
            "numero_nfse": _valor_xml(n["nfse_xml"], "nNFSe"), "valor": n["pedido"]["valor_centavos"] / 100,
            "tomador": (n["pedido"].get("tomador") or {}).get("nome", ""), "producao": bool(n["producao"]),
            # O que o botão "Repetir" do painel precisa para refazer a nota do mês seguinte (contrato recorrente).
            "servico": {k: n["pedido"].get(k, "") for k in ("codigo_servico", "nbs", "descricao", "competencia", "aliquota_simples")}
                       | {"tomador": n["pedido"].get("tomador"), "retencoes": n["pedido"].get("retencoes")},
            "ibscbs": n["pedido"].get("ibscbs"), "tributos": tributos_da_nota(n["nfse_xml"])}

    # --------------------------------------------------------------- cancelar

    def cancelar(self, usuario_id: str, empresa_id: str, nota_id: str, *, motivo_codigo: str, motivo: str,
                 mestra: bytes) -> dict:
        """Evento 101101 na Sefin. Usa o ambiente em que a NOTA saiu, não o do servidor: nota de teste se cancela no teste.
        Sem resposta do governo, consulta o evento antes de dizer qualquer coisa (pode ter sido registrado)."""
        emp = self.c.empresa(usuario_id, empresa_id)
        linha = self.c.bd.execute("select id from notas where id = ? and empresa_id = ?", (nota_id, empresa_id)).fetchone()
        if not linha:
            raise Erro(404, "nota_nao_encontrada", "nota não encontrada")
        nota = self.nota_por_id(nota_id)
        if nota["status"] == "cancelada":
            return self.publica(nota)
        if nota["status"] != "emitida" or not nota["chave"]:
            raise Erro(409, "nota_nao_emitida", "só nota emitida pode ser cancelada")
        if len((motivo or "").strip()) < 15:
            raise Erro(422, "motivo_curto", "descreva o motivo do cancelamento (15 caracteres ou mais)")
        producao = bool(nota["producao"])
        try:
            xml = dps.cancelamento(producao=producao, cnpj=emp["cnpj"], chave=nota["chave"], motivo_codigo=str(motivo_codigo),
                                   motivo=motivo, em=self.agora() - ATRASO_RELOGIO)
        except dps.DpsInvalida as e:
            raise Erro(422, "cancelamento_invalido", str(e)) from None
        pfx, senha = self.c.abrir_certificado(usuario_id, empresa_id, mestra)
        sefin = self.fabrica_sefin(producao=producao, pfx=pfx, senha=senha)
        chave_privada, cert, _ = pkcs12.load_key_and_certificates(pfx, senha.encode())
        r = sefin.enviar_evento(nota["chave"], dps.assinar(xml, chave_privada, cert))
        if r.alcancou and r.status < 300:
            return self.publica(self._salvar(nota_id, status="cancelada", erros=[]))
        confere = sefin.consultar_evento(nota["chave"], dps.EVENTO_CANCELAMENTO)
        if confere.alcancou and confere.status == 200:
            return self.publica(self._salvar(nota_id, status="cancelada", erros=[]))
        if not r.alcancou or r.status >= 500:
            raise Erro(502, "governo_sem_resposta", "o governo não respondeu; a nota segue emitida. Tente de novo.")
        erros = r.erros()
        raise Erro(422, "cancelamento_recusado", "; ".join(f"{e['codigo']} {e['mensagem']}" for e in erros) or "recusado")

    # --------------------------------------------------------------- emitir

    def emitir(self, usuario_id: str, empresa_id: str, pedido: dict, *, limite_mes: int | None, mestra: bytes) -> dict:
        emp = self.c.empresa(usuario_id, empresa_id)
        ref = str(pedido.get("ref") or secrets.token_urlsafe(12))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", ref):
            raise Erro(422, "ref_invalido", "ref aceita letras, números e _ . : - (até 64)")
        existente = self.c.bd.execute("select id from notas where empresa_id = ? and ref = ?", (empresa_id, ref)).fetchone()
        if existente:
            nota = self.nota_por_id(existente[0])
            if nota["status"] in ("emitida", "cancelada"):
                return self.publica(nota)
        pedido = self._preparar(emp, pedido)
        if pedido["producao"] and not self.producao:
            raise Erro(403, "producao_desligada", "este servidor só emite nota de teste (sem valor fiscal)")
        if existente and bool(self.nota_por_id(existente[0])["producao"]) != pedido["producao"]:
            # O mesmo ref em outro ambiente seria outra nota: reaproveitar a linha trocaria teste por fiscal no meio do caminho.
            raise Erro(409, "ref_em_outro_ambiente", "este ref já foi usado no outro ambiente; use um ref novo")
        pfx, senha = self.c.abrir_certificado(usuario_id, empresa_id, mestra)
        sefin = self.fabrica_sefin(producao=pedido["producao"], pfx=pfx, senha=senha)
        chave, cert, _ = pkcs12.load_key_and_certificates(pfx, senha.encode())
        if existente:
            nota = self.nota_por_id(existente[0])
            if nota["status"] == "desconhecida":
                achada = self._achar_emitida(sefin, nota)
                if isinstance(achada, dict):
                    return self.publica(self._salvar(nota["id"], status="emitida", **achada))
                if achada == "inalcancavel":
                    return self.publica(nota)
            if nota["status"] in ("recusada", "pendente"):
                self.c.consumir_nota(empresa_id, limite_mes)
                self._salvar(nota["id"], pedido=json.dumps(pedido, ensure_ascii=False), mes=mes_atual())
        else:
            self.c.consumir_nota(empresa_id, limite_mes)
            nota_id = f"nota_{secrets.token_urlsafe(12)}"
            numero = self._proximo_numero(empresa_id)  # fora da trava: ele mesmo a pega (a trava não é reentrante)
            with self.c.trava:
                self.c.bd.execute("insert into notas (id, empresa_id, ref, status, pedido, numero, producao, mes, criada, atualizada) "
                                  "values (?, ?, ?, 'pendente', ?, ?, ?, ?, ?, ?)",
                                  (nota_id, empresa_id, ref, json.dumps(pedido, ensure_ascii=False),
                                   numero, int(pedido["producao"]), mes_atual(), _agora(), _agora()))
            nota = self.nota_por_id(nota_id)
        return self.publica(self._enviar(sefin, emp, self.nota_por_id(nota["id"]), chave, cert))

    def _preparar(self, emp: dict, pedido: dict) -> dict:
        p = self._validar(pedido)
        if p["aliquota_simples"] and emp["regime"] != "simples":
            raise Erro(422, "aliquota_simples_invalida",
                       "alíquota do Simples só vale para ME/EPP do Simples; MEI e regime normal não informam")
        return p

    # --------------------------------------------------------------- conferir

    def conferir(self, usuario_id: str, empresa_id: str, pedido: dict) -> dict:
        """O que falta antes de emitir, tudo de uma vez: sem cota, sem certificado aberto e sem falar com o governo.

        Pedido do X (21/11): o agente tem de parar e perguntar ao usuário antes de emitir, não descobrir campo por campo
        a cada recusa. `faltando` bloqueia a emissão; `perguntar` não bloqueia, mas muda a nota (o agente confirma com a
        pessoa). A regra é a mesma do emitir (`_preparar` e `dps.montar`), nunca uma cópia dela."""
        emp = self.c.empresa(usuario_id, empresa_id)
        p = pedido if isinstance(pedido, dict) else {}
        faltando, perguntar = [], []

        def falta(campo: str, mensagem: str) -> None:
            faltando.append({"campo": campo, "mensagem": mensagem})

        if not emp.get("certificado"):
            falta("certificado", "suba o certificado A1 (e-CNPJ) da empresa no painel antes de emitir")
        for campo, msg in (("codigo_servico", "código de tributação nacional, 6 dígitos (ex.: 010101)"),
                           ("descricao", "descrição do serviço"), ("valor", "valor do serviço em reais (ex.: 1500,00)")):
            if not str(p.get(campo) or "").strip():
                falta(campo, msg)
        t = p.get("tomador")
        if isinstance(t, dict):
            if not re.sub(r"\D", "", str(t.get("documento") or "")):
                falta("tomador.documento", "CPF ou CNPJ do tomador")
            if not str(t.get("nome") or "").strip():
                falta("tomador.nome", "nome ou razão social do tomador")
            if any(str(t.get(k) or "").strip() for k in ENDERECO):
                for k in ("cep", "municipio_ibge", "logradouro", "numero", "bairro"):
                    if not str(t.get(k) or "").strip():
                        falta(f"tomador.{k}", "o endereço do tomador vai inteiro ou não vai (número: S/N quando não tem)")
        else:
            perguntar.append({"campo": "tomador", "mensagem": "nota sem tomador identificado; para empresa ou prefeitura, "
                                                              "informe CPF/CNPJ e nome"})
        ret = p.get("retencoes") if isinstance(p.get("retencoes"), dict) else {}
        if ret.get("iss_retido") and emp["regime"] == "simples" and not str(ret.get("aliquota_iss") or "").strip():
            falta("retencoes.aliquota_iss", "ISS retido de ME/EPP do Simples exige a alíquota do ISS (mínimo 1,8%)")
        if emp["regime"] == "simples" and not str(p.get("aliquota_simples") or "").strip():
            perguntar.append({"campo": "aliquota_simples", "mensagem": "alíquota do Simples (%) da empresa; sem ela a "
                                                                        "nota sai com 0%, como no emissor oficial em branco"})
        if not str(p.get("ambiente") or "").strip():
            perguntar.append({"campo": "ambiente", "mensagem": 'sem "ambiente" a nota sai de teste (sem valor fiscal); '
                                                                'para valer, "producao"'})
        if not faltando:  # o resto das regras: a mesma validação e a mesma DPS do emitir, montada sem enviar
            try:
                v = self._preparar(emp, p)
                agora = self.agora()
                self._montar(emp, v, numero=1, emitida_em=agora, producao=v["producao"])
                if v["producao"] and not self.producao:
                    falta("ambiente", "este servidor só emite nota de teste (sem valor fiscal)")
            except Erro as e:
                falta(e.codigo, str(e))
            except dps.DpsInvalida as e:
                falta("dps", str(e))
        return {"pronto": not faltando, "faltando": faltando, "perguntar": perguntar}

    def _montar(self, emp: dict, p: dict, *, numero: int, emitida_em: dt.datetime, producao: bool) -> tuple[str, str]:
        return dps.montar(
            producao=producao, prestador=self._prestador(emp, p), serie=SERIE, numero=numero,
            emitida_em=emitida_em, competencia=p["competencia"] or dps.hora_brasilia(emitida_em)[:10],
            codigo_servico=p["codigo_servico"], descricao=p["descricao"], valor_centavos=p["valor_centavos"], nbs=p["nbs"],
            tomador=dps.Tomador(**p["tomador"]) if p["tomador"] else None,
            ibscbs=dps.IbsCbs(**p["ibscbs"]) if p.get("ibscbs") else None,
            retencoes=dps.Retencoes(**p["retencoes"]) if p.get("retencoes") else None)

    def _validar(self, p: dict) -> dict:
        try:
            valor = round(numero_br(p.get("valor", "")) * 100)
        except ValueError:
            raise Erro(422, "valor_invalido", "valor do serviço em reais, ex.: 1500.00") from None
        tomador = p.get("tomador") or None
        competencia = str(p.get("competencia") or "")
        nbs = str(p.get("nbs") or "")
        return {"retencoes": self._retencoes(p.get("retencoes")),
                "aliquota_simples": _aliquota_simples(p.get("aliquota_simples")),
                "ibscbs": self._ibscbs(p.get("ibscbs"), nbs, competencia),
                "codigo_servico": str(p.get("codigo_servico", "")), "nbs": str(p.get("nbs") or ""),
                "descricao": str(p.get("descricao", "")), "valor_centavos": valor,
                "competencia": competencia, "producao": self._ambiente(p),
                "tomador": {"documento": str(tomador.get("documento", "")), "nome": str(tomador.get("nome", "")),
                            "email": str(tomador.get("email") or "")}
                           | {k: str(tomador.get(k) or "") for k in ENDERECO if tomador.get(k)}
                           if isinstance(tomador, dict) else None}

    @staticmethod
    def _retencoes(r) -> dict | None:
        if not r:
            return None
        if not isinstance(r, dict):
            raise Erro(422, "retencao_invalida", "retencoes é um objeto: iss_retido, aliquota_iss, irrf, csll, inss, pis, cofins")
        out = {k: _reais(r.get(k), k) for k in RETIDOS}
        out |= {"iss_retido": bool(r.get("iss_retido")), "aliquota_iss": str(r.get("aliquota_iss") or ""),
                "pis_cofins_retidos": bool(r.get("pis_cofins_retidos"))}
        return out if out["iss_retido"] or out["aliquota_iss"] or any(out[k] for k in RETIDOS) else None

    def _ibscbs(self, g, nbs: str, competencia: str) -> dict | None:
        """IBS/CBS vai na nota sempre que dá para classificar: com NBS, o motor oficial classifica; `false` desliga."""
        if g is False or str(g).lower() == "false":
            return None
        escolha = g if isinstance(g, dict) else {}
        if not nbs and not {"cst", "cclass_trib", "cind_op"} <= escolha.keys():
            return None
        data = competencia or dps.hora_brasilia(self.agora())[:10]
        classe = self.classificar(nbs, data, escolha)
        return {k: str(classe[k]) for k in ("cst", "cclass_trib", "cind_op")} | {
            "ind_final": "1" if str(escolha.get("ind_final", "0")) in ("1", "True", "true") else "0"}

    @staticmethod
    def _ambiente(p: dict) -> bool:
        ambiente = str(p.get("ambiente") or "teste")
        if ambiente not in ("teste", "producao"):
            raise Erro(422, "ambiente_invalido", 'ambiente é "teste" (sem valor fiscal) ou "producao" (valor fiscal)')
        return ambiente == "producao"

    def _prestador(self, emp: dict, p: dict | None = None) -> dps.Prestador:
        return dps.Prestador(cnpj=emp["cnpj"], municipio_ibge=str(emp["municipio_ibge"]), regime=emp["regime"],
                             inscricao_municipal=emp.get("inscricao_municipal") or "",
                             regime_especial=emp.get("regime_especial") or "0",
                             aliquota_simples=str((p or {}).get("aliquota_simples") or ""))

    def _enviar(self, sefin: Sefin, emp: dict, nota: dict, chave_privada, cert, colisoes: int = 0) -> dict:
        p = nota["pedido"]
        emitida_em = self.agora() - ATRASO_RELOGIO
        try:
            id_dps, xml = self._montar(emp, p, numero=nota["numero"], emitida_em=emitida_em, producao=bool(nota["producao"]))
        except dps.DpsInvalida as e:
            self.c.devolver_nota(emp["id"], nota["mes"])
            return self._salvar(nota["id"], status="recusada", erros=[{"codigo": "entrada", "mensagem": str(e)}])
        nota = self._salvar(nota["id"], id_dps=id_dps, status="desconhecida")
        r = sefin.enviar_dps(dps.assinar(xml, chave_privada, cert))
        if not r.alcancou or r.status >= 500:
            return nota  # sem resposta: pode ter saído; a próxima chamada consulta antes de reenviar
        chave = str(r.dados.get("chaveAcesso") or "")
        if r.status < 300 and chave:
            return self._salvar(nota["id"], status="emitida", chave=chave, nfse_xml=r.dados.get("nfseXml", ""), erros=[])
        erros = com_dicas(r.erros(), bool(nota["producao"]))
        if not any(e["codigo"] == DPS_DUPLICADA for e in erros):
            self.c.devolver_nota(emp["id"], nota["mes"])
            return self._salvar(nota["id"], status="recusada", erros=erros)
        achada = self._achar_emitida(sefin, nota)
        if achada == "inalcancavel":
            return nota
        if isinstance(achada, dict):
            return self._salvar(nota["id"], status="emitida", **achada)
        if colisoes >= MAX_COLISOES:
            self.c.devolver_nota(emp["id"], nota["mes"])
            return self._salvar(nota["id"], status="recusada", erros=erros)
        nota = self._salvar(nota["id"], numero=self._proximo_numero(emp["id"]))
        return self._enviar(sefin, emp, nota, chave_privada, cert, colisoes + 1)

    def _achar_emitida(self, sefin: Sefin, nota: dict):
        """A NFS-e já gerada para esta DPS, se for esta nota (mesmo valor e tomador)."""
        r = sefin.consultar_dps(nota["id_dps"])
        if not r.alcancou or (r.status >= 500):
            return "inalcancavel"
        if r.status == 404:
            return "nao_achou"
        chave = str(r.dados.get("chaveAcesso") or "")
        if r.status != 200 or not chave:
            return "inalcancavel"
        n = sefin.consultar_nfse(chave)
        xml = n.dados.get("nfseXml", "") if n.alcancou else ""
        if not xml:
            return {"chave": chave, "erros": []}
        p = nota["pedido"]
        mesmo_valor = _valor_xml(xml, "vServ") == f"{p['valor_centavos'] / 100:.2f}"
        doc = re.sub(r"\D", "", (p.get("tomador") or {}).get("documento", ""))
        mesmo_tomador = not doc or f">{doc}<" in xml
        return {"chave": chave, "nfse_xml": xml, "erros": []} if mesmo_valor and mesmo_tomador else "nao_e_nossa"
