"""NF-e de produto por empresa: cadastro fiscal (IE, endereço, série), numeração, emissão e as salvaguardas contra nota
dupla ou número queimado. A NF-e em si (XML, assinatura, SEFAZ da UF) é o `nfe.py`.

Salvaguardas (as mesmas da NFS-e, no vocabulário da NF-e):
  - `ref` de quem chama: o mesmo ref devolve a mesma nota, nunca uma segunda;
  - a chave e o XML assinado ficam gravados ANTES do envio; sem resposta da SEFAZ a nota fica DESCONHECIDA e a próxima
    chamada consulta a chave: autorizada vira emitida, "não consta" (217) reenvia o MESMO XML, nunca um número novo;
  - número já usado por outra nota do CNPJ (539, ex.: a série do Bling) pula para o seguinte, no máximo 5 vezes;
  - denegada (110, 301, 302) gasta o número e não reenvia; recusada devolve o número se ele foi o último reservado e
    devolve a cota do mês.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
from decimal import Decimal

from cryptography.hazmat.primitives.serialization import pkcs12

import nfe
from contas import Contas, Erro, _agora, mes_atual

ESQUEMA = """
create table if not exists nfe_cadastro (
  empresa_id text primary key references empresas(id) on delete cascade, dados text not null, atualizada text not null);
create table if not exists nfe_numeracao (
  empresa_id text not null references empresas(id) on delete cascade, producao integer not null, serie integer not null,
  ultimo integer not null, primary key (empresa_id, producao, serie));
create table if not exists nfe_notas (
  id text primary key, empresa_id text not null references empresas(id) on delete cascade, ref text not null,
  status text not null, pedido text not null, producao integer not null, serie integer not null, numero integer not null,
  chave text not null default '', xml_assinado text not null default '', xml text not null default '',
  protocolo text not null default '', erros text not null default '[]', mes text not null, criada text not null,
  atualizada text not null, unique (empresa_id, ref));
"""
CADASTRO = ("ie", "fantasia", "logradouro", "numero", "complemento", "bairro", "cep", "municipio", "uf", "telefone",
            "serie_teste", "serie_producao")
NAO_CONSTA = "217"
DUPLICIDADE_OUTRA = "539"  # número já usado por outra NF-e do CNPJ (chave diferente)
DUPLICIDADE_MESMA = "204"  # esta mesma nota já foi autorizada
DENEGADAS = {"110", "301", "302", "303"}
MAX_PULOS = 5
ATRASO_RELOGIO = dt.timedelta(seconds=60)


def _dig(t) -> str:
    return re.sub(r"\D", "", str(t or ""))


class EmissorNfe:
    def __init__(self, contas: Contas, *, producao: bool, fabrica_sefaz=nfe.Sefaz, agora=None):
        # `producao` = o servidor PERMITE nota com valor fiscal; cada pedido escolhe (`ambiente`, padrão "teste").
        self.c, self.producao, self.fabrica_sefaz = contas, producao, fabrica_sefaz
        self.agora = agora or (lambda: dt.datetime.now(dt.timezone.utc))
        self.c.bd.executescript(ESQUEMA)

    # ------------------------------------------------------------------ cadastro fiscal

    def cadastro(self, usuario_id: str, empresa_id: str) -> dict:
        emp = self.c.empresa(usuario_id, empresa_id)
        linha = self.c.bd.execute("select dados from nfe_cadastro where empresa_id = ?", (empresa_id,)).fetchone()
        dados = json.loads(linha[0]) if linha else {}
        uf = nfe.UF_DO_CUF.get(str(emp["municipio_ibge"])[:2], "")
        return {"empresa_id": empresa_id, "regime": emp["regime"], "municipio_ibge": str(emp["municipio_ibge"]), "uf": uf,
                "pronto": bool(dados.get("ie")) and emp["regime"] in nfe.CRT,
                "proximo_teste": self._ultimo(empresa_id, False, int(dados.get("serie_teste") or 1)) + 1,
                "proximo_producao": self._ultimo(empresa_id, True, int(dados.get("serie_producao") or 1)) + 1,
                **{k: dados.get(k, "") for k in CADASTRO}}

    def salvar_cadastro(self, usuario_id: str, empresa_id: str, d: dict) -> dict:
        emp = self.c.empresa(usuario_id, empresa_id)
        uf = nfe.UF_DO_CUF.get(str(emp["municipio_ibge"])[:2], "")
        dados = {k: str(d.get(k) or "").strip() for k in CADASTRO}
        dados["uf"] = uf  # a UF sai do município cadastrado: não se escolhe outra
        if not re.fullmatch(r"\d{2,14}", _dig(dados["ie"])):
            raise Erro(422, "ie_invalida", "inscrição estadual: só números (de 2 a 14)")
        for serie in ("serie_teste", "serie_producao"):
            dados[serie] = dados[serie] or "1"
            if not re.fullmatch(r"\d{1,3}", dados[serie]):
                raise Erro(422, "serie_invalida", "série da NF-e é de 0 a 999")
        end = self._endereco_emitente(emp, dados)
        try:
            end.validar("endereço da empresa")
        except nfe.NfeInvalida as e:
            raise Erro(422, "endereco_invalido", str(e)) from None
        with self.c.trava:
            self.c.bd.execute("insert into nfe_cadastro values (?, ?, ?) on conflict(empresa_id) do update set "
                              "dados = excluded.dados, atualizada = excluded.atualizada",
                              (empresa_id, json.dumps(dados, ensure_ascii=False), _agora()))
        # Quem já emite em outro sistema continua a numeração de lá: "próximo número" vale só para subir, nunca descer
        # (descer reusaria número que a SEFAZ já conhece e daria 539 em toda nota).
        for producao, chave in ((False, "proximo_teste"), (True, "proximo_producao")):
            if d.get(chave):
                self._definir_proximo(empresa_id, producao, int(dados["serie_producao" if producao else "serie_teste"]),
                                      d.get(chave))
        return self.cadastro(usuario_id, empresa_id)

    def _definir_proximo(self, empresa_id: str, producao: bool, serie: int, proximo) -> None:
        try:
            n = int(str(proximo))
        except ValueError:
            raise Erro(422, "numero_invalido", "próximo número é inteiro") from None
        if not (1 <= n <= 999_999_999):
            raise Erro(422, "numero_invalido", "próximo número de 1 a 999.999.999")
        atual = self._ultimo(empresa_id, producao, serie)
        if n - 1 < atual:
            raise Erro(409, "numero_para_tras", f"a série {serie} já está no número {atual}; o próximo só pode subir")
        with self.c.trava:
            self.c.bd.execute("insert into nfe_numeracao values (?, ?, ?, ?) on conflict(empresa_id, producao, serie) "
                              "do update set ultimo = excluded.ultimo", (empresa_id, int(producao), serie, n - 1))

    def _ultimo(self, empresa_id: str, producao: bool, serie: int) -> int:
        linha = self.c.bd.execute("select ultimo from nfe_numeracao where empresa_id = ? and producao = ? and serie = ?",
                                  (empresa_id, int(producao), serie)).fetchone()
        return linha[0] if linha else 0

    def _reservar(self, empresa_id: str, producao: bool, serie: int) -> int:
        with self.c.trava:
            self.c.bd.execute("insert or ignore into nfe_numeracao values (?, ?, ?, 0)", (empresa_id, int(producao), serie))
            self.c.bd.execute("update nfe_numeracao set ultimo = ultimo + 1 where empresa_id = ? and producao = ? and serie = ?",
                              (empresa_id, int(producao), serie))
            return self.c.bd.execute("select ultimo from nfe_numeracao where empresa_id = ? and producao = ? and serie = ?",
                                     (empresa_id, int(producao), serie)).fetchone()[0]

    def _devolver_numero(self, empresa_id: str, producao: bool, serie: int, numero: int) -> None:
        """Número de nota recusada volta só se foi o último reservado: no meio, devolver daria o mesmo número a duas."""
        with self.c.trava:
            self.c.bd.execute("update nfe_numeracao set ultimo = ultimo - 1 where empresa_id = ? and producao = ? "
                              "and serie = ? and ultimo = ?", (empresa_id, int(producao), serie, numero))

    @staticmethod
    def _endereco_emitente(emp: dict, d: dict) -> nfe.Endereco:
        return nfe.Endereco(logradouro=d.get("logradouro", ""), numero=d.get("numero", ""), bairro=d.get("bairro", ""),
                            municipio_ibge=str(emp["municipio_ibge"]), municipio=d.get("municipio", ""),
                            uf=nfe.UF_DO_CUF.get(str(emp["municipio_ibge"])[:2], ""), cep=d.get("cep", ""),
                            complemento=d.get("complemento", ""), telefone=d.get("telefone", ""))

    # ------------------------------------------------------------------ notas

    def nota_por_id(self, nota_id: str) -> dict:
        n = dict(self.c.bd.execute("select * from nfe_notas where id = ?", (nota_id,)).fetchone())
        n["pedido"], n["erros"] = json.loads(n["pedido"]), json.loads(n["erros"])
        return n

    def _salvar(self, nota_id: str, **campos) -> dict:
        campos["atualizada"] = _agora()
        if "erros" in campos:
            campos["erros"] = json.dumps(campos["erros"], ensure_ascii=False)
        with self.c.trava:
            self.c.bd.execute(f"update nfe_notas set {', '.join(f'{k} = ?' for k in campos)} where id = ?",
                              (*campos.values(), nota_id))
        return self.nota_por_id(nota_id)

    @staticmethod
    def publica(n: dict) -> dict:
        p = n["pedido"]
        return {k: n[k] for k in ("id", "ref", "status", "serie", "numero", "chave", "protocolo", "erros", "criada")} | {
            "producao": bool(n["producao"]), "valor": p.get("total"), "destinatario": p["destinatario"].get("nome", ""),
            "pedido": p}

    def notas(self, usuario_id: str, empresa_id: str, limite: int = 50) -> list[dict]:
        self.c.empresa(usuario_id, empresa_id)
        ids = self.c.bd.execute("select id from nfe_notas where empresa_id = ? order by criada desc limit ?",
                                (empresa_id, limite)).fetchall()
        return [self.publica(self.nota_por_id(i[0])) for i in ids]

    def xml(self, usuario_id: str, empresa_id: str, nota_id: str) -> tuple[str, str]:
        self.c.empresa(usuario_id, empresa_id)
        linha = self.c.bd.execute("select id from nfe_notas where id = ? and empresa_id = ?", (nota_id, empresa_id)).fetchone()
        n = self.nota_por_id(linha[0]) if linha else None
        if not n or not n["xml"]:
            raise Erro(404, "xml_indisponivel", "o XML existe só para nota autorizada")
        return n["chave"], n["xml"]

    # ------------------------------------------------------------------ emitir

    def emitir(self, usuario_id: str, empresa_id: str, pedido: dict, *, limite_mes: int | None, mestra: bytes) -> dict:
        emp = self.c.empresa(usuario_id, empresa_id)
        ref = str(pedido.get("ref") or secrets.token_urlsafe(12))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", ref):
            raise Erro(422, "ref_invalido", "ref aceita letras, números e _ . : - (até 64)")
        linha = self.c.bd.execute("select id from nfe_notas where empresa_id = ? and ref = ?", (empresa_id, ref)).fetchone()
        existente = self.nota_por_id(linha[0]) if linha else None
        if existente and existente["status"] in ("emitida", "denegada"):
            return self.publica(existente)
        cad = self.cadastro(usuario_id, empresa_id)
        if emp["regime"] not in nfe.CRT:
            raise Erro(422, "regime_sem_nfe", "NF-e por aqui é para Simples Nacional e MEI; regime normal ainda não")
        if not cad["pronto"]:
            raise Erro(409, "cadastro_nfe_incompleto", "preencha o cadastro da NF-e da empresa (IE e endereço) antes")
        p = self._validar(pedido)
        if p["producao"] and not self.producao:
            raise Erro(403, "producao_desligada", "este servidor só emite nota de teste (sem valor fiscal)")
        if existente and bool(existente["producao"]) != p["producao"]:
            raise Erro(409, "ref_em_outro_ambiente", "este ref já foi usado no outro ambiente; use um ref novo")
        pfx, senha = self.c.abrir_certificado(usuario_id, empresa_id, mestra)
        sefaz = self.fabrica_sefaz(uf=cad["uf"], producao=p["producao"], pfx=pfx, senha=senha)
        chave_priv, cert, _ = pkcs12.load_key_and_certificates(pfx, senha.encode())
        if existente and existente["status"] == "desconhecida":
            return self.publica(self._retomar(sefaz, emp, cad, existente, chave_priv, cert))
        serie = int(cad["serie_producao" if p["producao"] else "serie_teste"])
        if existente:  # recusada ou pendente: tenta de novo com o pedido novo, no mesmo número se ele ainda for dela
            self.c.consumir_nota(empresa_id, limite_mes)
            nota = self._salvar(existente["id"], pedido=json.dumps(p, ensure_ascii=False), mes=mes_atual(), status="pendente")
            if nota["serie"] != serie or self._ultimo(empresa_id, p["producao"], serie) < nota["numero"]:
                nota = self._salvar(nota["id"], serie=serie, numero=self._reservar(empresa_id, p["producao"], serie))
        else:
            self.c.consumir_nota(empresa_id, limite_mes)
            nota_id = f"nfe_{secrets.token_urlsafe(12)}"
            numero = self._reservar(empresa_id, p["producao"], serie)
            with self.c.trava:
                self.c.bd.execute("insert into nfe_notas (id, empresa_id, ref, status, pedido, producao, serie, numero, mes, "
                                  "criada, atualizada) values (?, ?, ?, 'pendente', ?, ?, ?, ?, ?, ?, ?)",
                                  (nota_id, empresa_id, ref, json.dumps(p, ensure_ascii=False), int(p["producao"]), serie,
                                   numero, mes_atual(), _agora(), _agora()))
            nota = self.nota_por_id(nota_id)
        return self.publica(self._enviar(sefaz, emp, cad, nota, chave_priv, cert))

    def _validar(self, p: dict) -> dict:
        ambiente = str(p.get("ambiente") or "teste")
        if ambiente not in ("teste", "producao"):
            raise Erro(422, "ambiente_invalido", 'ambiente é "teste" (sem valor fiscal) ou "producao" (valor fiscal)')
        dest = p.get("destinatario")
        itens = p.get("itens")
        if not isinstance(dest, dict) or not isinstance(itens, list) or not itens:
            raise Erro(422, "pedido_invalido", "a NF-e precisa de destinatario e de itens")
        try:
            venda_itens = [self._item(i) for i in itens]
            frete = nfe._dec(p.get("frete") or 0, "frete")
        except nfe.NfeInvalida as e:
            raise Erro(422, "nfe_invalida", str(e)) from None
        except (TypeError, AttributeError):
            raise Erro(422, "pedido_invalido", "cada item é um objeto com codigo, descricao, ncm, quantidade e valor_unitario") from None
        total = sum((i.total for i in venda_itens), Decimal(0)) + frete
        pags = p.get("pagamentos") or [{"forma": str(p.get("forma_pagamento") or "pix"), "valor": str(total)}]
        return {"producao": ambiente == "producao", "ambiente": ambiente, "itens": itens, "frete": str(frete),
                "total": f"{total:.2f}", "pagamentos": pags, "presencial": bool(p.get("presencial")),
                "natureza": str(p.get("natureza") or "VENDA DE MERCADORIA"), "informacoes": str(p.get("informacoes") or ""),
                "destinatario": {k: str(dest.get(k) or "") for k in ("documento", "nome", "ie", "email", "cep", "logradouro",
                                                                      "numero", "complemento", "bairro", "municipio_ibge",
                                                                      "municipio", "uf")}}

    @staticmethod
    def _item(i: dict) -> nfe.Item:
        return nfe.Item(codigo=str(i.get("codigo") or ""), descricao=str(i.get("descricao") or ""), ncm=str(i.get("ncm") or ""),
                        quantidade=nfe._dec(i.get("quantidade") or 1, "quantidade"),
                        valor_unitario=nfe._dec(i.get("valor_unitario"), "valor_unitario"), unidade=str(i.get("unidade") or "UN"),
                        gtin=str(i.get("gtin") or ""), cfop=str(i.get("cfop") or ""), csosn=str(i.get("csosn") or "102"),
                        origem=str(i.get("origem") or "0"), cest=str(i.get("cest") or ""))

    def _venda(self, emp: dict, cad: dict, nota: dict) -> nfe.Venda:
        p, d = nota["pedido"], nota["pedido"]["destinatario"]
        return nfe.Venda(
            emitente=nfe.Emitente(cnpj=emp["cnpj"], razao=emp["razao"], ie=cad["ie"], regime=emp["regime"],
                                  endereco=self._endereco_emitente(emp, cad), fantasia=cad.get("fantasia", "")),
            destinatario=nfe.Destinatario(documento=d["documento"], nome=d["nome"], ie=d["ie"], email=d["email"],
                                          endereco=nfe.Endereco(d["logradouro"], d["numero"], d["bairro"], d["municipio_ibge"],
                                                                d["municipio"], d["uf"].upper(), d["cep"], d["complemento"])),
            itens=[self._item(i) for i in p["itens"]],
            pagamentos=[nfe.Pagamento(str(x.get("forma") or ""), nfe._dec(x.get("valor"), "pagamento"),
                                      str(x.get("descricao") or "")) for x in p["pagamentos"]],
            serie=nota["serie"], numero=nota["numero"], producao=p["producao"],
            emitida_em=self.agora() - ATRASO_RELOGIO, frete=Decimal(p["frete"]), presencial=p["presencial"],
            natureza=p["natureza"], informacoes=p["informacoes"])

    def _recusar(self, emp: dict, nota: dict, erros: list[dict]) -> dict:
        self.c.devolver_nota(emp["id"], nota["mes"])
        self._devolver_numero(emp["id"], bool(nota["producao"]), nota["serie"], nota["numero"])
        return self._salvar(nota["id"], status="recusada", erros=erros)

    def _enviar(self, sefaz, emp: dict, cad: dict, nota: dict, chave_priv, cert, pulos: int = 0) -> dict:
        try:
            chave, xml = nfe.montar(self._venda(emp, cad, nota))
        except nfe.NfeInvalida as e:
            return self._recusar(emp, nota, [{"codigo": "entrada", "mensagem": str(e)}])
        except (ValueError, KeyError) as e:
            return self._recusar(emp, nota, [{"codigo": "entrada", "mensagem": f"dado inválido: {e}"}])
        assinado = nfe.assinar(xml, chave_priv, cert)
        # Gravado antes de enviar: sem resposta, a retomada consulta ESTA chave e reenvia ESTE XML.
        nota = self._salvar(nota["id"], chave=chave, xml_assinado=assinado, status="desconhecida")
        r = sefaz.autorizar(assinado, lote=str(secrets.randbelow(10**15)))
        return self._resultado(sefaz, emp, cad, nota, r, chave_priv, cert, pulos)

    def _resultado(self, sefaz, emp, cad, nota, r: nfe.Retorno, chave_priv, cert, pulos: int) -> dict:
        if not r.alcancou:
            return nota
        if r.autorizada:
            return self._salvar(nota["id"], status="emitida", protocolo=r.protocolo, erros=[],
                                xml=nfe.nfe_proc(nota["xml_assinado"], r.prot_xml))
        if r.cstat in DENEGADAS:
            return self._salvar(nota["id"], status="denegada", erros=[{"codigo": r.cstat, "mensagem": r.motivo}])
        if r.cstat == DUPLICIDADE_MESMA:
            return self._retomar(sefaz, emp, cad, nota, chave_priv, cert)
        if r.cstat == DUPLICIDADE_OUTRA and pulos < MAX_PULOS:
            nota = self._salvar(nota["id"], numero=self._reservar(emp["id"], bool(nota["producao"]), nota["serie"]))
            return self._enviar(sefaz, emp, cad, nota, chave_priv, cert, pulos + 1)
        return self._recusar(emp, nota, [{"codigo": r.cstat or "sefaz", "mensagem": r.motivo or "recusada"}])

    def _retomar(self, sefaz, emp, cad, nota, chave_priv, cert) -> dict:
        """Nota sem resposta: pergunta pela chave antes de qualquer coisa."""
        q = sefaz.consultar(nota["chave"])
        if not q.alcancou:
            return nota
        if q.autorizada:
            return self._salvar(nota["id"], status="emitida", protocolo=q.protocolo, erros=[],
                                xml=nfe.nfe_proc(nota["xml_assinado"], q.prot_xml))
        if q.cstat == NAO_CONSTA:
            r = sefaz.autorizar(nota["xml_assinado"], lote=str(secrets.randbelow(10**15)))
            return self._resultado(sefaz, emp, cad, nota, r, chave_priv, cert, 0)
        return nota
