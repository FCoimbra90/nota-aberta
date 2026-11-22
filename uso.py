"""Painel de uso da Nota Aberta, no modelo do painel do Fichário (apps/fichario/painel.js): contadores por minuto,
canais, quem usa (pessoa, agente de IA, robô), de onde vem o tráfego e o que já se emitiu. Sem dado pessoal: nada de
IP, e-mail ou CNPJ guardado. O visitante único do dia é um HMAC do IP com uma chave que muda a cada dia e só existe
no servidor; o hash não volta a ser IP e não liga um dia ao outro.

Por que existe: no lançamento pelo X (20/11/2026) chegaram 30 mil pedidos em 4 h e duas pessoas disseram que "o link
não abre". Sem número por minuto e por origem não dava para saber se era o site, o navegador do X ou o link do post.

Gravação: contadores em memória, despejados no SQLite a cada 15 s (e antes de cada leitura do painel), para não pagar
uma escrita em disco por pedido. Retenção: 8 dias por minuto, 31 dias nos rankings.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
CANAIS = ("pagina", "calculadora", "conta", "emissao", "mcp", "estatico", "outro")
QUEM = ("pessoa", "agente", "robo")
COLUNAS = ("total", *QUEM, *CANAIS, "s2", "s3", "s4", "s5", "lat_soma")
DESPEJO_S = 15
MIN_RANKING = 2  # chave com menos ocorrências no dia não sai no painel (cauda que pode identificar alguém)

ESQUEMA = f"""
create table if not exists uso_min (minuto text primary key, {", ".join(c + " integer not null default 0" for c in COLUNAS)});
create table if not exists uso_top (dia text not null, tipo text not null, chave text not null, n integer not null,
  primary key (dia, tipo, chave));
create table if not exists uso_vis (dia text not null, h text not null, primary key (dia, h));
"""

_AGENTE = re.compile(r"ChatGPT-User|ClaudeBot|Claude-User|GPTBot|Google-Extended|anthropic-ai|OpenAI|PerplexityBot|"
                     r"Bytespider|meta-externalagent|cohere-ai|mcp|modelcontextprotocol|Cursor/|Continue\.dev|"
                     r"python-requests|python-urllib|httpx|aiohttp|node-fetch|undici|axios|curl/|Wget", re.IGNORECASE)
_ROBO = re.compile(r"bot\b|Googlebot|Bingbot|DuckDuckBot|Slurp|Yandex|Baiduspider|Applebot|facebookexternalhit|"
                   r"Twitterbot|LinkedInBot|SemrushBot|AhrefsBot|DotBot|PetalBot|crawler|spider|kube-probe|"
                   r"Uptime|Pingdom|HeadlessChrome|WhatsApp|TelegramBot|Discordbot|Slackbot", re.IGNORECASE)
_ESTATICO = re.compile(r"\.(css|js|svg|webp|png|jpg|ico|txt|json|xml|woff2?)$")
_EMISSAO = re.compile(r"^/conta/empresas/[^/]+/(notas|nfe)$")
_FERRAMENTA = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def quem(ua: str) -> str:
    """O X, o Instagram e o WhatsApp abrem o link no navegador do app: isso é pessoa. Já o robô que gera a prévia
    do link (Twitterbot, facebookexternalhit, WhatsApp) é robô."""
    u = ua or ""
    if not u.strip():
        return "agente"
    if _AGENTE.search(u):
        return "agente"
    if _ROBO.search(u) and not re.search(r"Twitter for|Instagram|FBAN|FBAV", u):
        return "robo"
    return "pessoa"


def canal(metodo: str, caminho: str) -> str | None:
    """None = não conta (sonda do Kubernetes e o próprio painel, que se atualiza sozinho a cada 5 s)."""
    if caminho == "/health" or caminho.startswith("/operacao"):
        return None
    if caminho in ("/", "") or caminho.endswith(".html"):
        return "pagina"
    if caminho.startswith("/api/"):
        return "calculadora"
    if caminho == "/mcp":
        return "mcp"
    if metodo == "POST" and _EMISSAO.match(caminho):
        return "emissao"
    if caminho.startswith(("/conta/", "/entrar/", "/sair")):
        return "conta"
    if _ESTATICO.search(caminho):
        return "estatico"
    return "outro"


def origem(referer: str, proprio: str) -> str:
    """Host de quem mandou a visita; o próprio site e a falta de Referer viram 'direto'. t.co é o encurtador do X."""
    try:
        host = (urllib.parse.urlsplit(referer).hostname or "").lower()
    except ValueError:
        return "direto"
    if not host or host == proprio:
        return "direto"
    host = host.removeprefix("www.").removeprefix("m.").removeprefix("l.").removeprefix("lm.")
    return {"t.co": "x.com (t.co)", "twitter.com": "x.com"}.get(host, host)[:60]


def _minuto(agora: float) -> str:
    return dt.datetime.fromtimestamp(agora, TZ).strftime("%Y-%m-%dT%H:%M")


class Uso:
    def __init__(self, caminho: str | Path, segredo: bytes, *, host: str = "reforma.genesisinnovation.io",
                 negocio: Callable[[], dict] | None = None, relogio: Callable[[], float] = time.time):
        self.bd = sqlite3.connect(str(caminho), check_same_thread=False, isolation_level=None)
        self.bd.executescript("pragma journal_mode=wal;" + ESQUEMA)
        self.segredo, self.host, self.negocio, self.relogio = segredo, host, negocio, relogio
        self.trava = threading.Lock()
        self.min: dict[str, Counter] = {}
        self.top: Counter = Counter()
        self.vis: set[tuple[str, str]] = set()
        self.ultimo_despejo = relogio()

    # ---- gravação -------------------------------------------------------------------------------------------------
    def registrar(self, metodo: str, caminho: str, status: int, lat_ms: float, cab: dict, ip: str = "",
                  ferramentas: list[str] | None = None) -> None:
        c = canal(metodo, caminho)
        if c is None:
            return
        agora = self.relogio()
        m = _minuto(agora)
        dia = m[:10]
        q = quem(cab.get("user-agent", ""))
        with self.trava:
            k = self.min.setdefault(m, Counter())
            k["total"] += 1
            k[q] += 1
            k[c] += 1
            k[f"s{min(max(status // 100, 2), 5)}"] += 1
            k["lat_soma"] += round(lat_ms)
            if c == "pagina" and status == 200:
                self.top[(dia, "pagina", caminho if caminho not in ("", "/") else "/")] += 1
                if q == "pessoa":
                    self.top[(dia, "origem", origem(cab.get("referer", ""), self.host))] += 1
                    pais = (cab.get("cf-ipcountry") or "").upper()
                    if re.fullmatch(r"[A-Z]{2}", pais):
                        self.top[(dia, "pais", pais)] += 1
                    if ip:
                        chave = hmac.new(self.segredo, dia.encode(), hashlib.sha256).digest()
                        self.vis.add((dia, hmac.new(chave, ip.encode(), hashlib.sha256).hexdigest()[:16]))
            for f in ferramentas or []:
                if _FERRAMENTA.match(f):
                    self.top[(dia, "ferramenta", f)] += 1
        if agora - self.ultimo_despejo >= DESPEJO_S:
            self.despejar()

    def despejar(self) -> None:
        with self.trava:
            mins, tops, vis = self.min, self.top, self.vis
            self.min, self.top, self.vis = {}, Counter(), set()
            self.ultimo_despejo = self.relogio()
        if not (mins or tops or vis):
            return
        cols = ", ".join(COLUNAS)
        soma = ", ".join(f"{c}={c}+excluded.{c}" for c in COLUNAS)
        with self.bd:
            self.bd.execute("begin")
            for m, k in mins.items():
                self.bd.execute(f"insert into uso_min (minuto, {cols}) values (?{', ?' * len(COLUNAS)}) "
                                f"on conflict(minuto) do update set {soma}", (m, *(k[c] for c in COLUNAS)))
            for (dia, tipo, chave), n in tops.items():
                self.bd.execute("insert into uso_top values (?,?,?,?) on conflict do update set n=n+excluded.n",
                                (dia, tipo, chave, n))
            self.bd.executemany("insert or ignore into uso_vis values (?,?)", list(vis))
            corte = _minuto(self.relogio() - 8 * 86400)
            self.bd.execute("delete from uso_min where minuto < ?", (corte,))
            self.bd.execute("delete from uso_vis where dia < ?", (corte[:10],))
            self.bd.execute("delete from uso_top where dia < ?", (_minuto(self.relogio() - 31 * 86400)[:10],))

    # ---- leitura --------------------------------------------------------------------------------------------------
    def _linhas(self, sql: str, args=()) -> list[dict]:
        cur = self.bd.execute(sql, args)
        nomes = [d[0] for d in cur.description]
        return [dict(zip(nomes, r, strict=True)) for r in cur.fetchall()]

    def estatisticas(self) -> dict:
        self.despejar()
        agora = self.relogio()
        m60, h24, d7 = (_minuto(agora - s) for s in (3600, 86400, 7 * 86400))
        hoje = _minuto(agora)[:10]
        soma = ", ".join(f"coalesce(sum({c}),0) {c}" for c in COLUNAS)
        dia = self._linhas(f"select {soma} from uso_min where minuto >= ?", (hoje,))[0]
        dia["lat_media_ms"] = round(dia.pop("lat_soma") / dia["total"]) if dia["total"] else 0
        ult5 = self._linhas("select coalesce(sum(total),0) n from uso_min where minuto > ?", (_minuto(agora - 300),))[0]
        top = {t: self._linhas("select chave, n from uso_top where dia=? and tipo=? and n>=? order by n desc limit 12",
                               (hoje, t, MIN_RANKING)) for t in ("origem", "pais", "pagina", "ferramenta")}
        vis = self._linhas("select dia, count(*) n from uso_vis where dia >= ? group by dia order by dia", (d7[:10],))
        return {
            "gerado_em": dt.datetime.fromtimestamp(agora, dt.UTC).isoformat(),
            "rpm": round(ult5["n"] / 5, 1),
            "hoje": dia,
            "visitantes": {"hoje": next((v["n"] for v in vis if v["dia"] == hoje), 0), "d7": vis},
            "timeline": {
                "m60": self._linhas("select minuto bucket, total, pessoa from uso_min where minuto >= ? order by 1", (m60,)),
                "h24": self._linhas("select substr(minuto,1,13)||':00' bucket, sum(total) total, sum(pessoa) pessoa "
                                    "from uso_min where minuto >= ? group by 1 order by 1", (h24,)),
                "d7": self._linhas("select substr(minuto,1,10) bucket, sum(total) total, sum(pessoa) pessoa "
                                   "from uso_min where minuto >= ? group by 1 order by 1", (d7,)),
            },
            "top": top,
            "negocio": self.negocio() if self.negocio else None,
        }


def negocio_do_banco(caminho: str | Path, relogio: Callable[[], float] = time.time) -> Callable[[], dict]:
    """Contagens do banco das contas, só números: quantas contas, empresas, certificados e notas (por ambiente e
    situação). Abre em só leitura para o painel nunca segurar escrita da emissão."""
    def ler() -> dict:
        hoje = _minuto(relogio())[:10]
        try:
            bd = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            return {"erro": "banco das contas indisponível"}
        try:
            def um(sql, args=()):
                try:
                    return bd.execute(sql, args).fetchone()[0] or 0
                except sqlite3.Error:  # tabela ainda não criada (emissor nunca rodou)
                    return 0

            def notas(tabela):
                try:
                    linhas = bd.execute(f"select producao, status, count(*), sum(substr(criada,1,10) >= ?) from {tabela} "
                                        "group by 1, 2", (hoje,)).fetchall()
                except sqlite3.Error:
                    linhas = []
                out = {"producao": {}, "teste": {}, "hoje": 0}
                for prod, st, n, nh in linhas:
                    out["producao" if prod else "teste"][st] = n
                    out["hoje"] += nh or 0
                return out
            def linhas(sql, args=()):
                try:
                    return bd.execute(sql, args).fetchall()
                except sqlite3.Error:
                    return []

            return {
                "funil": {
                    "contas": um("select count(*) from usuarios"),
                    "com_empresa": um("select count(distinct usuario_id) from empresas"),
                    "com_certificado": um("select count(distinct e.usuario_id) from empresas e join certificados c "
                                          "on c.empresa_id = e.id"),
                    # Etapa = qualquer nota aceita (teste OU valor fiscal): quem vai direto à nota fiscal pula o
                    # teste, e um funil com "teste" antes de "produção" mostrava 200% de conversão (21/11).
                    "emitiu": um("select count(distinct e.usuario_id) from empresas e join notas n on "
                                 "n.empresa_id = e.id where n.status in ('emitida', 'cancelada')"),
                    "emitiu_producao": um("select count(distinct e.usuario_id) from empresas e join notas n on "
                                          "n.empresa_id = e.id where n.producao = 1 and n.status in ('emitida', 'cancelada')"),
                },
                "lista": [_empresa_do_painel(*r) for r in linhas(SQL_EMPRESAS)],
                "contas": um("select count(*) from usuarios"),
                "contas_hoje": um("select count(*) from usuarios where substr(criado,1,10) >= ?", (hoje,)),
                "empresas": um("select count(*) from empresas"),
                "certificados": um("select count(*) from certificados"),
                "chaves_api": um("select count(*) from chaves"),
                "nfse": notas("notas"),
                "nfe": notas("nfe_notas"),
            }
        finally:
            bd.close()
    return ler


# Uma linha por empresa para o dono acompanhar quem está usando e onde trava. Razão social, cidade (IBGE) e regime são
# dados da empresa; e-mail, CNPJ e dados do tomador NUNCA entram (o painel é para suporte, não para cadastro).
SQL_EMPRESAS = """
select e.razao, e.municipio_ibge, e.regime, substr(e.criado, 1, 10),
  exists(select 1 from certificados c where c.empresa_id = e.id),
  (select count(*) from notas n where n.empresa_id = e.id and n.producao = 0 and n.status in ('emitida', 'cancelada')),
  (select count(*) from notas n where n.empresa_id = e.id and n.producao = 1 and n.status in ('emitida', 'cancelada')),
  (select count(*) from notas n where n.empresa_id = e.id and n.status = 'recusada'),
  (select count(*) from nfe_notas f where f.empresa_id = e.id),
  (select max(n.criada) from notas n where n.empresa_id = e.id),
  (select n.erros from notas n where n.empresa_id = e.id and n.status = 'recusada' order by n.criada desc limit 1)
from empresas e order by 7 desc, 6 desc, 10 desc, e.criado desc limit 200"""


def _empresa_do_painel(razao, ibge, regime, criada, cert, teste, producao, recusadas, nfe, ultima, erros) -> dict:
    try:
        ultimo = (json.loads(erros) or [{}])[0] if erros else {}
    except (ValueError, TypeError):
        ultimo = {}
    etapa = ("emitindo com valor fiscal" if producao else "emitindo de teste" if teste else
             "travou no envio" if recusadas else "com certificado" if cert else "só cadastro")
    return {"razao": (razao or "")[:80], "municipio_ibge": str(ibge or ""), "regime": regime or "", "criada": criada or "",
            "certificado": bool(cert), "nfse_teste": teste, "nfse_producao": producao, "recusadas": recusadas, "nfe": nfe,
            "ultima": (ultima or "")[:16], "etapa": etapa,
            "ultimo_erro": _explicado(ultimo, producao > 0) if ultimo else None}


def _explicado(erro: dict, producao: bool) -> dict:
    """A mesma frase e dica que a pessoa vê no painel dela (emissor.explicar); sem o emissor, só o texto limpo."""
    try:
        import emissor
        e = emissor.explicar(erro, producao)
    except Exception:  # noqa: BLE001 (painel de leitura não pode cair por causa da explicação)
        e = {"codigo": erro.get("codigo"), "mensagem": _sem_html(str(erro.get("mensagem") or ""))}
    return {"codigo": str(e.get("codigo") or "")[:20], "mensagem": str(e.get("mensagem") or "")[:200],
            "dica": str(e.get("dica") or "")[:300]}


def _sem_html(texto: str) -> str:
    """O 403 do governo vem como página HTML inteira; o painel mostra só o texto."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|\\[rn]", " ", texto)).strip()


def autorizado(email: str, lista: str) -> bool:
    """`NOTA_PAINEL_EMAILS` = e-mails separados por vírgula (segredo do pod, não Git). Vazia = ninguém entra."""
    email = (email or "").strip().lower()
    return bool(email) and email in {e.strip().lower() for e in (lista or "").split(",") if e.strip()}
