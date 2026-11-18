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


def _valor_xml(xml: str, tag: str) -> str:
    achado = re.search(f"<{tag}>([^<]*)</{tag}>", xml or "")
    return achado.group(1) if achado else ""


class Emissor:
    def __init__(self, contas: Contas, *, producao: bool, fabrica_sefin=Sefin, agora=None):
        self.c, self.producao, self.fabrica_sefin = contas, producao, fabrica_sefin
        self.agora = agora or (lambda: dt.datetime.now(dt.timezone.utc))
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
        return {k: n[k] for k in ("id", "ref", "status", "numero", "chave", "erros", "criada", "atualizada")} | {
            "numero_nfse": _valor_xml(n["nfse_xml"], "nNFSe"), "valor": n["pedido"]["valor_centavos"] / 100,
            "tomador": (n["pedido"].get("tomador") or {}).get("nome", ""), "producao": bool(n["producao"])}

    # --------------------------------------------------------------- emitir

    def emitir(self, usuario_id: str, empresa_id: str, pedido: dict, *, limite_mes: int, mestra: bytes) -> dict:
        emp = self.c.empresa(usuario_id, empresa_id)
        ref = str(pedido.get("ref") or secrets.token_urlsafe(12))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", ref):
            raise Erro(422, "ref_invalido", "ref aceita letras, números e _ . : - (até 64)")
        existente = self.c.bd.execute("select id from notas where empresa_id = ? and ref = ?", (empresa_id, ref)).fetchone()
        if existente:
            nota = self.nota_por_id(existente[0])
            if nota["status"] in ("emitida", "cancelada"):
                return self.publica(nota)
        pedido = self._validar(pedido)
        pfx, senha = self.c.abrir_certificado(usuario_id, empresa_id, mestra)
        sefin = self.fabrica_sefin(producao=self.producao, pfx=pfx, senha=senha)
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
                                   numero, int(self.producao), mes_atual(), _agora(), _agora()))
            nota = self.nota_por_id(nota_id)
        return self.publica(self._enviar(sefin, emp, self.nota_por_id(nota["id"]), chave, cert))

    def _validar(self, p: dict) -> dict:
        try:
            valor = round(float(str(p.get("valor", "")).replace(",", ".")) * 100)
        except ValueError:
            raise Erro(422, "valor_invalido", "valor do serviço em reais, ex.: 1500.00") from None
        tomador = p.get("tomador") or None
        return {"codigo_servico": str(p.get("codigo_servico", "")), "nbs": str(p.get("nbs") or ""),
                "descricao": str(p.get("descricao", "")), "valor_centavos": valor,
                "competencia": str(p.get("competencia") or ""),
                "tomador": {"documento": str(tomador.get("documento", "")), "nome": str(tomador.get("nome", "")),
                            "email": str(tomador.get("email") or "")} if isinstance(tomador, dict) else None}

    def _prestador(self, emp: dict) -> dps.Prestador:
        return dps.Prestador(cnpj=emp["cnpj"], municipio_ibge=str(emp["municipio_ibge"]), regime=emp["regime"])

    def _enviar(self, sefin: Sefin, emp: dict, nota: dict, chave_privada, cert, colisoes: int = 0) -> dict:
        p = nota["pedido"]
        emitida_em = self.agora() - ATRASO_RELOGIO
        try:
            id_dps, xml = dps.montar(
                producao=self.producao, prestador=self._prestador(emp), serie=SERIE, numero=nota["numero"],
                emitida_em=emitida_em, competencia=p["competencia"] or dps.hora_brasilia(emitida_em)[:10],
                codigo_servico=p["codigo_servico"], descricao=p["descricao"], valor_centavos=p["valor_centavos"], nbs=p["nbs"],
                tomador=dps.Tomador(**p["tomador"]) if p["tomador"] else None)
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
        erros = r.erros()
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
