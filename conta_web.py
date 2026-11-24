"""Rotas da conta: login com Google, empresas, certificado, emissão, notas, chaves de API e plano.

Autenticação: cookie de sessão `na_sessao` (HttpOnly, Secure, SameSite=Lax) para a página, ou
`Authorization: Bearer na_...` para sistema e agente. Toda escrita pelo cookie exige o cabeçalho `X-Nota: 1`, que um
formulário de outro site não consegue mandar (CSRF); pela chave de API ele não é preciso.

Ambiente da emissão: produção restrita (sem valor fiscal) por padrão. Produção só com `NOTA_PRODUCAO=1` no servidor.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import urllib.parse
from http.cookies import CookieError, SimpleCookie

import cofre
import oauth
import planos as planos_mod
from contas import Contas, Erro
from emissor import Emissor
from emissor_nfe import EmissorNfe

COOKIE = "na_sessao"
COOKIE_OAUTH = "na_oauth"
COOKIE_VOLTA = "na_volta"
VOLTAS = ("/operacao",)


class Resultado:
    def __init__(self, status: int, corpo: object = None, *, redirecionar: str = "", cookies: list[str] | None = None,
                 bruto: bytes | None = None, tipo: str = "", arquivo: str = ""):
        self.status, self.corpo, self.redirecionar, self.cookies = status, corpo, redirecionar, cookies or []
        self.bruto, self.tipo, self.arquivo = bruto, tipo, arquivo  # arquivo: nome do download (só [A-Za-z0-9.-])


def danfe(nfe_proc: str) -> bytes:
    """DANFE (PDF da NF-e) pela BrazilFiscalReport (LGPL, a mesma do emissor da casa), a partir do nfeProc autorizado."""
    try:
        from brazilfiscalreport.danfe import Danfe
    except ImportError:
        raise Erro(503, "danfe_desligado", "o gerador de DANFE não está instalado neste servidor; baixe o XML") from None
    return bytes(Danfe(xml=nfe_proc).output())


def danfse(nfse_xml: str, env: dict) -> bytes:
    """PDF oficial da NFS-e pelo DANFSe Service da Receita (Go), que roda ao lado em DANFSE_URL."""
    import urllib.error
    import urllib.request
    base = env.get("DANFSE_URL", "")
    if not base:
        raise Erro(503, "danfse_desligado", "o gerador de PDF não está ligado neste servidor")
    req = urllib.request.Request(base.rstrip("/") + "/api/danfse", data=nfse_xml.encode(), method="POST",
                                 headers={"Content-Type": "application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except (urllib.error.URLError, OSError):
        raise Erro(502, "danfse_falhou", "o gerador de PDF não respondeu; tente de novo") from None


def _cookie(nome: str, valor: str, idade: int) -> str:
    return f"{nome}={valor}; Path=/; Max-Age={idade}; HttpOnly; Secure; SameSite=Lax"


class Conta:
    def __init__(self, contas: Contas, *, base: str, planos: planos_mod.Planos | None = None, emissor: Emissor | None = None,
                 emissor_nfe: EmissorNfe | None = None,
                 env: dict | None = None):
        self.c, self.base, self.env = contas, base.rstrip("/"), env if env is not None else os.environ
        self.planos = planos or planos_mod.Planos()
        self.emissor = emissor or Emissor(contas, producao=self.env.get("NOTA_PRODUCAO") == "1")
        self.emissor_nfe = emissor_nfe or EmissorNfe(contas, producao=self.emissor.producao)

    # ---------------------------------------------------------------- portas

    def tratar(self, metodo: str, caminho: str, consulta: str, cab: dict, corpo: bytes) -> Resultado:
        try:
            return self._rotear(metodo, caminho, urllib.parse.parse_qs(consulta), cab, corpo)
        except Erro as e:
            return Resultado(e.status, {"erro": {"codigo": e.codigo, "mensagem": e.mensagem}})
        except cofre.CofreIndisponivel:
            return Resultado(503, {"erro": {"codigo": "cofre_indisponivel", "mensagem": "o cofre de certificados está fora do ar"}})

    def _cookies(self, cab: dict) -> dict:
        jar = SimpleCookie()
        try:
            jar.load(cab.get("cookie", ""))
        except CookieError:  # cookie malformado de outro site no mesmo domínio: ignora
            return {}
        return {k: m.value for k, m in jar.items()}

    def _quem(self, cab: dict) -> tuple[dict | None, bool]:
        """(usuário, veio por chave de API)."""
        aut = cab.get("authorization", "")
        if aut.lower().startswith("bearer "):
            u = self.c.usuario_da_chave(aut[7:].strip())
            if not u:
                raise Erro(401, "chave_invalida", "chave de API inválida ou revogada")
            return u, True
        return self.c.usuario_da_sessao(self._cookies(cab).get(COOKIE, "")), False

    def _exigir(self, cab: dict, metodo: str) -> dict:
        u, por_chave = self._quem(cab)
        if not u:
            raise Erro(401, "sem_sessao", "entre com o Google ou use uma chave de API")
        if metodo != "GET" and not por_chave and cab.get("x-nota") != "1":
            raise Erro(403, "csrf", "pedido sem o cabeçalho X-Nota")
        return u

    @staticmethod
    def _json(corpo: bytes) -> dict:
        try:
            d = json.loads(corpo or b"{}")
        except ValueError:
            raise Erro(400, "json_invalido", "corpo não é JSON") from None
        if not isinstance(d, dict):
            raise Erro(400, "json_invalido", "corpo precisa ser um objeto")
        return d

    def _mestra(self) -> bytes:
        return cofre.chave_mestra(self.env)

    # ---------------------------------------------------------------- rotas

    def _rotear(self, m: str, caminho: str, q: dict, cab: dict, corpo: bytes) -> Resultado:
        if caminho == "/entrar/google" and m == "GET":
            if not oauth.configurado("google"):
                raise Erro(503, "login_indisponivel", "o login com Google ainda não está configurado neste servidor")
            url, estado = oauth.iniciar("google", self.base)
            # Volta depois do login só para destinos da lista (nunca um endereço livre: seria redirecionamento aberto).
            volta = q.get("volta", [""])[0]
            cookies = [_cookie(COOKIE_OAUTH, estado, 600)] + ([_cookie(COOKIE_VOLTA, volta.strip("/"), 600)]
                                                              if volta in VOLTAS else [])
            return Resultado(302, redirecionar=url, cookies=cookies)
        if caminho == "/entrar/google/retorno" and m == "GET":
            estado, _, verif = self._cookies(cab).get(COOKIE_OAUTH, "").partition(".")
            if not estado or q.get("state", [""])[0] != estado:
                raise Erro(400, "estado_invalido", "o login expirou ou veio de outra aba; tente de novo")
            ident = oauth.identidade("google", q.get("code", [""])[0], self.base, verif)
            u = self.c.entrar_google(ident)
            volta = "/" + self._cookies(cab).get(COOKIE_VOLTA, "")
            return Resultado(302, redirecionar=volta if volta in VOLTAS else "/painel.html",
                             cookies=[_cookie(COOKIE, self.c.emitir_sessao(u["id"]), 14 * 24 * 3600), _cookie(COOKIE_OAUTH, "", 0),
                                      _cookie(COOKIE_VOLTA, "", 0)])
        if caminho == "/entrar/local" and m == "GET":
            token = q.get("t", [""])[0]
            if not self.c.usuario_da_sessao(token):
                raise Erro(401, "sessao_invalida", "acesso local inválido ou vencido: rode o instalar-emissor.command de novo")
            # Sem Secure: o painel roda em http://localhost (fora do Chrome, o Safari recusa cookie Secure em http).
            return Resultado(302, redirecionar="/painel.html",
                             cookies=[f"{COOKIE}={token}; Path=/; Max-Age={14 * 24 * 3600}; HttpOnly; SameSite=Lax"])
        if caminho == "/sair":
            return Resultado(302, redirecionar="/", cookies=[_cookie(COOKIE, "", 0)])
        if caminho == "/conta/planos" and m == "GET":
            return Resultado(200, {"planos": [p | {"checkout": planos_mod.link_checkout(p)} for p in self.planos.lista],
                                   "sob_medida": planos_mod.sob_medida(), "producao": self.emissor.producao})

        u = self._exigir(cab, m)
        partes = [p for p in caminho.split("/") if p][1:]  # sem o "conta"
        if partes == ["eu"] and m == "GET":
            plano = self.planos.do_email(u["email"])
            return Resultado(200, {"usuario": u, "plano": plano, "empresas": self.c.empresas(u["id"]),
                                   "chaves": self.c.chaves(u["id"]), "producao": self.emissor.producao,
                                   "login_google": oauth.configurado("google")})
        if partes == ["plano", "atualizar"] and m == "POST":
            self.planos.esquecer(u["email"])
            return Resultado(200, {"plano": self.planos.do_email(u["email"])})
        if partes == ["empresas"] and m == "POST":
            d = self._json(corpo)
            plano = self.planos.do_email(u["email"])
            if plano["empresas"] is not None and len(self.c.empresas(u["id"])) >= plano["empresas"]:
                raise Erro(402, "limite_empresas", f"o plano {plano['nome']} permite {plano['empresas']} empresa(s)")
            try:
                ibge = int(d.get("municipio_ibge", 0))
            except (TypeError, ValueError):
                raise Erro(422, "municipio_invalido", "código IBGE do município tem 7 dígitos") from None
            return Resultado(201, self.c.cadastrar_empresa(u["id"], str(d.get("cnpj", "")), str(d.get("razao", "")),
                                                            ibge, str(d.get("regime", "simples")),
                                                            str(d.get("inscricao_municipal") or ""),
                                                            str(d.get("regime_especial") or "0")))
        if len(partes) >= 2 and partes[0] == "empresas":
            return self._empresa(m, u, partes[1], partes[2:], corpo)
        if partes == ["chaves"] and m == "POST":
            return Resultado(201, {"chave": self.c.criar_chave(u["id"], str(self._json(corpo).get("nome", ""))),
                                   "aviso": "guarde agora: ela não aparece de novo"})
        if len(partes) == 2 and partes[0] == "chaves" and m == "DELETE":
            return Resultado(200, {"revogadas": self.c.revogar_chave(u["id"], partes[1])})
        raise Erro(404, "rota_inexistente", "rota da conta não existe")

    def _empresa(self, m: str, u: dict, eid: str, resto: list[str], corpo: bytes) -> Resultado:
        if not re.fullmatch(r"emp_[A-Za-z0-9_-]{1,40}", eid):
            raise Erro(404, "empresa_nao_encontrada", "empresa não encontrada")
        if resto == [] and m == "GET":
            return Resultado(200, self.c.empresa(u["id"], eid))
        if resto == [] and m == "PATCH":  # só inscricao_municipal e regime_especial; o resto do corpo é ignorado
            d = self._json(corpo)
            return Resultado(200, self.c.editar_empresa(u["id"], eid, {k: d[k] for k in ("inscricao_municipal", "regime_especial")
                                                                     if k in d}))
        if resto == ["certificado"] and m == "POST":
            d = self._json(corpo)
            try:
                pfx = base64.b64decode(str(d.get("pfx_base64", "")), validate=True)
            except (binascii.Error, ValueError):
                raise Erro(422, "pfx_invalido", "envie o arquivo .pfx em base64") from None
            return Resultado(201, self.c.guardar_certificado(u["id"], eid, pfx, str(d.get("senha", "")), self._mestra()))
        if resto == ["certificado"] and m == "DELETE":
            return Resultado(200, {"apagado": self.c.apagar_certificado(u["id"], eid)})
        if resto == ["notas"] and m == "GET":
            return Resultado(200, {"notas": self.emissor.notas(u["id"], eid)})
        if resto == ["notas", "conferir"] and m == "POST":
            return Resultado(200, self.emissor.conferir(u["id"], eid, self._json(corpo)))
        if resto == ["notas"] and m == "POST":
            plano = self.planos.do_email(u["email"])
            nota = self.emissor.emitir(u["id"], eid, self._json(corpo), limite_mes=plano["notas_mes"], mestra=self._mestra())
            return Resultado(201 if nota["status"] == "emitida" else 202, nota)
        if len(resto) == 3 and resto[0] == "notas" and resto[2] == "cancelar" and m == "POST":
            d = self._json(corpo)
            return Resultado(200, self.emissor.cancelar(u["id"], eid, resto[1], motivo_codigo=str(d.get("motivo_codigo", "1")),
                                                        motivo=str(d.get("motivo", "")), mestra=self._mestra()))
        if len(resto) == 3 and resto[0] == "notas" and resto[2] == "pdf" and m == "GET":
            self.c.empresa(u["id"], eid)
            linha = self.c.bd.execute("select nfse_xml from notas where id = ? and empresa_id = ? and status = 'emitida'",
                                      (resto[1], eid)).fetchone()
            if not linha or not linha[0]:
                raise Erro(404, "nota_sem_xml", "nota não emitida ou sem o XML da NFS-e")
            return Resultado(200, bruto=danfse(linha[0], self.env), tipo="application/pdf")
        if len(resto) == 3 and resto[0] == "notas" and resto[2] == "arquivar" and m == "POST":
            return self._arquivar(u, eid, resto[1], self._json(corpo))
        if resto and resto[0] == "nfe":
            return self._nfe(m, u, eid, resto[1:], corpo)
        raise Erro(404, "rota_inexistente", "rota da empresa não existe")

    # Nome do arquivo arquivado: só um nome de PDF, nunca um caminho (sem barra, sem começar por ponto).
    NOME_ARQUIVO = re.compile(r"[0-9A-Za-zÀ-ÿ][0-9A-Za-zÀ-ÿ .,()_-]{0,119}\.pdf")

    def _arquivar(self, u: dict, eid: str, nota_id: str, d: dict) -> Resultado:
        """Versão pessoal: grava o PDF oficial de uma nota emitida na pasta montada em NOTA_ARQUIVO
        (no Mac, só a pasta "NFs Emitidas"). Não sobrescreve arquivo existente sem "substituir": true."""
        from pathlib import Path
        pasta = self.env.get("NOTA_ARQUIVO", "")
        if not pasta or not Path(pasta).is_dir():
            raise Erro(503, "arquivo_desligado", "a pasta de arquivo não está ligada neste servidor (NOTA_ARQUIVO)")
        nome = str(d.get("nome", "")).strip()
        if not self.NOME_ARQUIVO.fullmatch(nome):
            raise Erro(422, "nome_invalido", "informe só o nome do PDF, ex.: NF 2026.12.pdf (sem pastas)")
        self.c.empresa(u["id"], eid)
        linha = self.c.bd.execute("select nfse_xml from notas where id = ? and empresa_id = ? and status = 'emitida'",
                                  (nota_id, eid)).fetchone()
        if not linha or not linha[0]:
            raise Erro(404, "nota_sem_xml", "nota não emitida ou sem o XML da NFS-e")
        destino = Path(pasta) / nome
        if destino.exists() and not d.get("substituir"):
            raise Erro(409, "arquivo_existe", f"já existe {nome} na pasta; mande \"substituir\": true para trocar")
        pdf = danfse(linha[0], self.env)
        provisorio = destino.with_name("." + nome + ".parcial")
        provisorio.write_bytes(pdf)
        os.replace(provisorio, destino)
        return Resultado(201, {"arquivo": nome, "bytes": len(pdf)})

    def _nfe(self, m: str, u: dict, eid: str, resto: list[str], corpo: bytes) -> Resultado:
        """NF-e de produto: /conta/empresas/<eid>/nfe[/cadastro | /<nota>/xml | /<nota>/pdf]."""
        e = self.emissor_nfe
        if resto == ["cadastro"] and m == "GET":
            return Resultado(200, e.cadastro(u["id"], eid))
        if resto == ["cadastro"] and m == "PUT":
            return Resultado(200, e.salvar_cadastro(u["id"], eid, self._json(corpo)))
        if resto == [] and m == "GET":
            return Resultado(200, {"notas": e.notas(u["id"], eid)})
        if resto == [] and m == "POST":
            plano = self.planos.do_email(u["email"])
            nota = e.emitir(u["id"], eid, self._json(corpo), limite_mes=plano["notas_mes"], mestra=self._mestra())
            return Resultado(201 if nota["status"] == "emitida" else 202, nota)
        if len(resto) == 2 and resto[1] in ("xml", "pdf") and m == "GET":
            chave, xml = e.xml(u["id"], eid, resto[0])
            if resto[1] == "xml":
                return Resultado(200, bruto=xml.encode(), tipo="application/xml", arquivo=f"{chave}-nfe.xml")
            return Resultado(200, bruto=danfe(xml), tipo="application/pdf", arquivo=f"{chave}-danfe.pdf")
        raise Erro(404, "rota_inexistente", "rota da NF-e não existe")
