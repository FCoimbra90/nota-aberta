"""nota-aberta: a calculadora oficial da Reforma Tributária do Consumo (CBS, IBS e IS) aberta ao público.

O cálculo NÃO é nosso: é o motor que a Receita Federal publicou em MIT
(github.com/ReceitaFederal/calculadora-rtc-regime-regular), compilado sem alteração no Dockerfile
a partir de um commit fixo. Reescrever a fórmula seria uma segunda cópia da regra fiscal, que
diverge da oficial na primeira atualização; rodando o jar deles, o número é o deles por construção.

Este processo é só a porta:
  - sobe o jar oficial em 127.0.0.1:8081 e morre junto se ele morrer (o pod reinicia);
  - serve a página (web/) em / e a sonda em /health;
  - repassa /api/* ao jar, só nas rotas de leitura e de cálculo (lista PERMITIDAS), com corpo
    limitado e teto de pedidos por IP, porque o serviço é público e sem login.

Só biblioteca padrão: a imagem já carrega uma JVM, não precisa de mais nada.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import docs as docs_mod
import mcp
import uso as uso_mod


class ServidorFila(ThreadingHTTPServer):
    # Fila de conexões à espera do accept: o padrão do socketserver é 5, e com 25 pessoas chegando juntas o kernel derruba
    # o excedente e o cliente só tenta de novo em 1 s (p95 de 1,1 s medido em 21/11, apps/factory/bin/medir-carga.py).
    request_queue_size = 128


AQUI = Path(__file__).resolve().parent
WEB = AQUI / "web"
BACKEND = os.environ.get("RTC_BACKEND", "http://127.0.0.1:8081")
PORTA = int(os.environ.get("PORT", "8080"))
CORPO_MAX = 256 * 1024          # uma nota com centenas de itens cabe; um upload de lixo não
TETO_POR_MINUTO = int(os.environ.get("RTC_TETO_MINUTO", "120"))
TEMPO_BACKEND = 30

# (método, regex do caminho depois de /api). O resto do jar (actuator, xml de exemplo) não sai.
PERMITIDAS = [
    ("GET", re.compile(r"^/calculadora/dados-abertos/[A-Za-z0-9/_\-]+$")),
    ("GET", re.compile(r"^/calculadora/xml/[A-Za-z0-9/_\-]+$")),
    ("POST", re.compile(r"^/calculadora/(observabilidade/)?regime-geral$")),
    ("POST", re.compile(r"^/calculadora/base-calculo/[A-Za-z0-9_\-]+$")),
    ("POST", re.compile(r"^/calculadora/nfse/[A-Za-z0-9/_\-]+$")),
    # Consultas da NFS-e (classificação por NBS, indicador de operação, locais): dados abertos, só leitura.
    ("GET", re.compile(r"^/calculadora/nfse/(situacoes-classificacoes-tributarias|indicador-operacao|local-operacao|"
                       r"locais-fornecimento|locais-incidencia)$")),
    ("POST", re.compile(r"^/calculadora/xml/(generate|validate)$")),
    ("GET", re.compile(r"^/(swagger-ui\.html|swagger-ui/[A-Za-z0-9._\-/]+|api-docs(/[A-Za-z0-9._\-]+)?)$")),
]
TIPOS = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8",
         ".json": "application/json", ".ico": "image/x-icon", ".webp": "image/webp", ".xml": "application/xml",
         ".woff2": "font/woff2"}  # fontes da Genesis na página (21/11); como octet-stream + nosniff o Firefox recusa a fonte
CABECALHOS_SEGURANCA = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}
# A página só carrega o próprio JS/CSS; o Swagger do jar usa script e estilo inline, por isso
# a CSP estrita vale só para a nossa página.
CSP_PAGINA = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
              "connect-src 'self' https://viacep.com.br; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


_ASSET_LOCAL = re.compile(rb'(href|src)="([A-Za-z0-9_\-]+\.(?:css|js))"')


def carimbar(html: bytes) -> bytes:
    """Põe ?v=<hash do conteúdo> em cada CSS e JS local que a página pede.

    A zona da Cloudflare reescreve o Cache-Control do CSS para max-age=14400 (Browser Cache TTL de 4 h, medido em
    21/11): depois de uma publicação, o navegador do X ficou com o landing.css velho e a página nova saiu sem estilo.
    Com o hash no endereço, arquivo novo é endereço novo e nenhum cache serve o antigo."""
    def troca(m: re.Match) -> bytes:
        arq = WEB / m.group(2).decode()
        if not arq.is_file():
            return m.group(0)
        return m.group(1) + b'="' + m.group(2) + b"?v=" + versao(arq).encode() + b'"'
    return _ASSET_LOCAL.sub(troca, html)


_VERSOES: dict[tuple[str, float], str] = {}


def versao(arq: Path) -> str:
    """Hash curto do conteúdo, lembrado por (arquivo, mtime): a página não relê e re-hasheia o CSS a cada visita."""
    chave = (str(arq), arq.stat().st_mtime)
    if chave not in _VERSOES:
        _VERSOES[chave] = hashlib.sha256(arq.read_bytes()).hexdigest()[:10]
    return _VERSOES[chave]


# Cache em camadas (21/11, "pra escalar tem que cachear muita coisa"). Medido antes: HTML e API DYNAMIC na Cloudflare,
# todo cálculo indo ao motor Java. Agora:
#  - CSS/JS com ?v=<hash certo>: imutável por 1 ano (arquivo novo = endereço novo);
#  - páginas: 1 min no navegador e 5 min na borda, servindo a velha por até 1 dia enquanto revalida;
#  - dados abertos e consultas de NFS-e do motor (GET): 1 h no navegador, 1 dia na borda;
#  - cálculos (POST): memória do processo por 1 h, porque o motor é determinístico para o mesmo corpo e a mesma versão.
CACHE_IMUTAVEL = "public, max-age=31536000, immutable"
CACHE_PAGINA = "public, max-age=60, s-maxage=300, stale-while-revalidate=86400"
CACHE_API_GET = "public, max-age=3600, s-maxage=86400"
CACHE_ARQUIVO = "public, max-age=3600"


class MemoriaDeCalculo:
    """Resposta do motor oficial por (rota, corpo), com prazo e teto em bytes. Só guarda 200."""

    def __init__(self, segundos: int = 3600, teto_bytes: int = 32 * 1024 * 1024, relogio=time.monotonic):
        self.segundos, self.teto, self.relogio = segundos, teto_bytes, relogio
        self.itens: dict[str, tuple[float, bytes, str]] = {}
        self.bytes = 0
        self.trava = threading.Lock()

    @staticmethod
    def chave(api: str, corpo: bytes) -> str:
        return api + "|" + hashlib.sha256(corpo).hexdigest()

    def ler(self, chave: str) -> tuple[bytes, str] | None:
        with self.trava:
            item = self.itens.get(chave)
            if item is None:
                return None
            if self.relogio() - item[0] > self.segundos:
                self._tirar(chave)
                return None
            self.itens[chave] = self.itens.pop(chave)  # mais recente vai para o fim (LRU)
            return item[1], item[2]

    def guardar(self, chave: str, dados: bytes, tipo: str) -> None:
        if len(dados) > self.teto // 8:
            return
        with self.trava:
            if chave in self.itens:
                self._tirar(chave)
            self.itens[chave] = (self.relogio(), dados, tipo)
            self.bytes += len(dados)
            while self.bytes > self.teto:
                self._tirar(next(iter(self.itens)))

    def _tirar(self, chave: str) -> None:
        _, dados, _ = self.itens.pop(chave)
        self.bytes -= len(dados)


MEMORIA = MemoriaDeCalculo()


def permitida(metodo: str, caminho_api: str) -> bool:
    if ".." in caminho_api or "//" in caminho_api:
        return False
    return any(m == metodo and r.match(caminho_api) for m, r in PERMITIDAS)


class Teto:
    """Janela de 60 s por IP. Em memória: um pod só, e reiniciar zera (aceitável para um teto)."""

    def __init__(self, por_minuto: int, relogio=time.monotonic):
        self.por_minuto, self.relogio, self.janelas, self.trava = por_minuto, relogio, {}, threading.Lock()

    def aceita(self, ip: str) -> bool:
        agora = self.relogio()
        with self.trava:
            inicio, n = self.janelas.get(ip, (agora, 0))
            if agora - inicio >= 60:
                inicio, n = agora, 0
            if n >= self.por_minuto:
                self.janelas[ip] = (inicio, n)
                return False
            self.janelas[ip] = (inicio, n + 1)
            if len(self.janelas) > 50_000:  # não deixa a tabela crescer sem fim
                self.janelas = {k: v for k, v in self.janelas.items() if agora - v[0] < 60}
            return True


TETO = Teto(TETO_POR_MINUTO)
CONTA = None  # conta_web.Conta quando o servidor tem banco e segredo (montar_conta); sem eles, só a calculadora
USO = None  # uso.Uso quando há banco e segredo (montar_uso): o painel de uso em /operacao
ROTAS_CONTA = ("/entrar/", "/sair", "/conta/")


def montar_conta(env: dict | None = None):
    """A conta só sobe com `NOTA_DB` (arquivo SQLite no disco persistente) e `NOTA_SEGREDO_SESSAO` (32+ bytes)."""
    env = env if env is not None else os.environ
    if not env.get("NOTA_DB") or len(env.get("NOTA_SEGREDO_SESSAO", "")) < 32:
        return None
    import conta_web
    import contas
    return conta_web.Conta(contas.Contas(env["NOTA_DB"], env["NOTA_SEGREDO_SESSAO"].encode()),
                           base=env.get("NOTA_BASE_URL", "https://reforma.genesisinnovation.io"), env=env)


def montar_uso(env: dict | None = None):
    """O painel de uso grava ao lado do banco das contas (`NOTA_USO_DB`, padrão `uso.db` na mesma pasta)."""
    env = env if env is not None else os.environ
    if not env.get("NOTA_DB") or len(env.get("NOTA_SEGREDO_SESSAO", "")) < 32:
        return None
    caminho = env.get("NOTA_USO_DB") or str(Path(env["NOTA_DB"]).with_name("uso.db"))
    host = (env.get("NOTA_BASE_URL", "https://reforma.genesisinnovation.io").split("//")[-1]).split("/")[0]
    return uso_mod.Uso(caminho, env["NOTA_SEGREDO_SESSAO"].encode(), host=host,
                       negocio=uso_mod.negocio_do_banco(env["NOTA_DB"]))


def ferramentas_mcp(corpo) -> list[str]:
    """Nome das ferramentas chamadas num pedido MCP (um ou um lote); o resto do pedido não é guardado."""
    pedidos = corpo if isinstance(corpo, list) else [corpo]
    return [str(p["params"].get("name", "")) for p in pedidos if isinstance(p, dict) and p.get("method") == "tools/call"
            and isinstance(p.get("params"), dict)]


def chamar_motor(metodo: str, api: str, dados: bytes | None) -> tuple[int, object]:
    """Uma chamada ao jar oficial, devolvida como (status, JSON). Usada pelo MCP; a API repassa os bytes crus."""
    req = urllib.request.Request(BACKEND + "/api" + api, data=dados, method=metodo,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TEMPO_BACKEND) as r:
            status, corpo = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, corpo = e.code, e.read()
    except (urllib.error.URLError, OSError):
        return 502, None
    try:
        return status, json.loads(corpo or b"null")
    except ValueError:
        return status, {"bruto": corpo[:500].decode("utf-8", "replace")}


def backend_vivo() -> bool:
    try:
        with urllib.request.urlopen(BACKEND + "/api/calculadora/dados-abertos/versao", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


class Porta(BaseHTTPRequestHandler):
    server_version = "nota-aberta"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # Sem timeout, sockets que mandam meio cabeçalho seguram uma thread cada para sempre (slowloris; medido no
    # agenda-clinica, que é o mesmo servidor da biblioteca padrão).
    timeout = 30

    def log_message(self, fmt, *args):  # sem IP no log: não precisamos dele guardado
        sys.stderr.write(f"{self.command} {self.path.split('?')[0]}\n")

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def parse_request(self):
        # O relógio da latência começa com o pedido lido, não antes: numa conexão keep-alive o
        # handle_one_request fica parado esperando o PRÓXIMO pedido, e esse tempo ocioso virava
        # "latência média" de 3 a 5 s no painel (medido em 21/11, com o servidor respondendo em ms).
        self._inicio = time.monotonic()
        return super().parse_request()

    def handle_one_request(self):
        # Cada pedido vira contador do painel de uso depois de respondido; falha do painel nunca derruba o pedido.
        self._status, self._ferramentas, self._inicio = 0, None, None
        super().handle_one_request()
        if USO is None or not getattr(self, "command", None) or not self._status or self._inicio is None:
            return
        try:
            USO.registrar(self.command, self.path.split("?")[0], self._status, (time.monotonic() - self._inicio) * 1000,
                          {k.lower(): v for k, v in self.headers.items()}, self._ip(), self._ferramentas)
        except Exception as e:  # noqa: BLE001 (contador é acessório; o erro vai ao log e o serviço segue)
            sys.stderr.write(f"painel de uso: {type(e).__name__}\n")

    def _ip(self) -> str:
        return (self.headers.get("CF-Connecting-IP") or self.client_address[0] or "?").strip()

    def _enviar(self, status: int, corpo: bytes, tipo: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        for k, v in {**CABECALHOS_SEGURANCA, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _json(self, status: int, obj) -> None:
        self._enviar(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                     {"Cache-Control": "no-store"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        caminho = self.path.split("?")[0]
        if caminho == "/health":
            ok = backend_vivo()
            return self._enviar(200 if ok else 503, b"ok" if ok else b"motor fora", "text/plain; charset=utf-8",
                                {"Cache-Control": "no-store"})
        if caminho in ("/api", "/api/", "/api/docs", "/doc", "/documentacao"):
            return self._enviar(302, b"", "text/plain; charset=utf-8", {"Location": "/docs"})
        if caminho in ("/docs", "/llms.txt"):
            return self._docs(caminho)
        if caminho == "/mcp":
            return self._mcp_get()
        if caminho.startswith("/api/"):
            return self._repassar()
        if caminho.startswith(ROTAS_CONTA):
            return self._conta()
        if caminho in ("/operacao", "/operacao/stats.json"):
            return self._operacao(caminho)
        return self._estatico(caminho)

    def do_POST(self):
        caminho = self.path.split("?")[0]
        if caminho.startswith("/api/"):
            return self._repassar()
        if caminho == "/mcp":
            return self._mcp()
        if caminho.startswith(ROTAS_CONTA):
            return self._conta()
        self._json(405, {"erro": "método não permitido"})

    def do_DELETE(self):
        if self.path.split("?")[0].startswith("/conta/"):
            return self._conta()
        self._json(405, {"erro": "método não permitido"})

    def do_PATCH(self):  # editar a empresa (PATCH /conta/empresas/<id>: inscrição municipal e regime especial)
        if self.path.split("?")[0].startswith("/conta/"):
            return self._conta()
        self._json(405, {"erro": "método não permitido"})

    def do_PUT(self):  # cadastro da NF-e da empresa (PUT /conta/empresas/<id>/nfe/cadastro)
        if self.path.split("?")[0].startswith("/conta/"):
            return self._conta()
        self._json(405, {"erro": "método não permitido"})

    def _conta(self):
        if CONTA is None:
            return self._json(503, {"erro": {"codigo": "conta_desligada", "mensagem": "cadastro e emissão ainda não estão ligados neste servidor"}})
        if not TETO.aceita(self._ip()):
            return self._json(429, {"erro": f"mais de {TETO.por_minuto} pedidos por minuto deste IP"})
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._json(400, {"erro": "Content-Length inválido"})
        if n > CORPO_MAX:
            return self._json(413, {"erro": f"corpo acima de {CORPO_MAX // 1024} KB"})
        corpo = self.rfile.read(n) if n else b""
        caminho, _, consulta = self.path.partition("?")
        cab = {k.lower(): v for k, v in self.headers.items()}
        r = CONTA.tratar(self.command, caminho, consulta, cab, corpo)
        self.send_response(r.status)
        for c in r.cookies:
            self.send_header("Set-Cookie", c)
        for k, v in {**CABECALHOS_SEGURANCA, "Cache-Control": "no-store"}.items():
            self.send_header(k, v)
        if r.bruto is not None:
            self.send_header("Content-Type", r.tipo)
            self.send_header("Content-Length", str(len(r.bruto)))
            if getattr(r, "arquivo", "") and re.fullmatch(r"[A-Za-z0-9.-]{1,80}", r.arquivo):
                self.send_header("Content-Disposition", f'attachment; filename="{r.arquivo}"')
            self.end_headers()
            self.wfile.write(r.bruto)
            return None
        if r.redirecionar:
            self.send_header("Location", r.redirecionar)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        dados = json.dumps(r.corpo, ensure_ascii=False).encode()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)
        return None

    def _mcp(self):
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._json(400, {"erro": "Content-Length inválido"})
        if n > CORPO_MAX:
            return self._json(413, {"erro": f"corpo acima de {CORPO_MAX // 1024} KB"})
        try:
            corpo = json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON inválido"}})
        ip = self._ip()
        self._ferramentas = ferramentas_mcp(corpo)

        def chamar(metodo: str, api: str, dados: bytes | None):
            # O teto vale por chamada ao motor, não por requisição: um lote de MCP não dribla o limite por IP.
            if not TETO.aceita(ip):
                return 429, {"erro": f"mais de {TETO.por_minuto} pedidos por minuto deste IP"}
            return chamar_motor(metodo, api, dados)

        conta = None
        aut = self.headers.get("Authorization", "")
        if CONTA is not None and aut.lower().startswith("bearer "):
            cab_conta = {"authorization": aut}

            def conta(metodo: str, caminho: str, pedido: dict | None):
                # Mesmo teto por IP da API: cada chamada à conta pelo MCP conta como um pedido.
                if not TETO.aceita(ip):
                    return 429, {"erro": f"mais de {TETO.por_minuto} pedidos por minuto deste IP"}
                corpo_conta = json.dumps(pedido).encode() if pedido is not None else b""
                r = CONTA.tratar(metodo, caminho, "", cab_conta, corpo_conta)
                return r.status, r.corpo

        status, resp = mcp.responder(corpo, chamar, conta)
        if resp is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self._json(status, resp)

    def _docs(self, caminho: str):
        """Documentação gerada do código (docs.py): a mesma lista de ferramentas que o MCP expõe."""
        base = os.environ.get("NOTA_BASE_URL", "https://reforma.genesisinnovation.io").rstrip("/")
        if caminho == "/llms.txt":
            return self._enviar(200, docs_mod.markdown(base).encode(), TIPOS[".txt"], {"Cache-Control": CACHE_PAGINA})
        self._enviar(200, carimbar(docs_mod.pagina(base).encode()), TIPOS[".html"],
                     {"Cache-Control": CACHE_PAGINA, "Content-Security-Policy": CSP_PAGINA})

    def _mcp_get(self):
        """GET /mcp. Cliente MCP que abre o fluxo SSE (Accept: text/event-stream) recebe 405, como manda a spec do
        Streamable HTTP para servidor sem SSE. Navegador vai para a documentação (antes: 404 "não encontrado",
        reclamação no X em 21/11). Outro cliente recebe um JSON dizendo como usar."""
        aceita = self.headers.get("Accept", "")
        if "text/event-stream" in aceita:
            return self._enviar(405, b"", "text/plain; charset=utf-8", {"Allow": "POST"})
        if "text/html" in aceita:
            return self._enviar(302, b"", "text/plain; charset=utf-8", {"Location": "/docs#mcp"})
        self._json(200, {"mcp": "Nota Aberta", "transporte": "Streamable HTTP: POST com JSON-RPC 2.0 neste endereço",
                         "documentacao": "/docs#mcp", "texto_para_agentes": "/llms.txt"})

    def _operacao(self, caminho: str):
        """Painel de uso: só para quem entrou com Google e está em NOTA_PAINEL_EMAILS (segredo do pod)."""
        if USO is None or CONTA is None:
            return self._json(503, {"erro": "painel de uso desligado neste servidor"})
        cab = {k.lower(): v for k, v in self.headers.items()}
        u = CONTA.c.usuario_da_sessao(CONTA._cookies(cab).get("na_sessao", ""))
        ok = bool(u) and uso_mod.autorizado(u.get("email", ""), os.environ.get("NOTA_PAINEL_EMAILS", ""))
        if caminho.endswith(".json"):
            return self._json(200, USO.estatisticas()) if ok else self._json(403, {"erro": "sem acesso ao painel"})
        nome = "operacao.html" if ok else "operacao-entrar.html"
        corpo = carimbar((WEB / nome).read_bytes())
        self._enviar(200 if ok else 403, corpo, TIPOS[".html"], {"Content-Security-Policy": CSP_PAGINA,
                                                                 "Cache-Control": "no-store"})

    def _estatico(self, caminho: str):
        nome = "index.html" if caminho in ("/", "") else caminho.lstrip("/")
        # Só a raiz de web/ e a pasta img/ (fotos da página); nada de "..", barra dupla ou outra pasta.
        # operacao*.html só sai pela rota /operacao, que confere o login.
        if not re.fullmatch(r"(img/)?[A-Za-z0-9_\-]+(\.[A-Za-z0-9]+)", nome) or re.fullmatch(r"operacao.*\.html", nome):
            return self._json(404, {"erro": "não encontrado"})
        arq = WEB / nome
        if not arq.is_file():
            return self._json(404, {"erro": "não encontrado"})
        # Foto não muda de conteúdo sem mudar de nome: cache longo. CSS/JS só é imutável quando o ?v= é o hash do
        # conteúdo atual (endereço sem ou com hash velho revalida, senão um HTML antigo prenderia o arquivo errado).
        v = (self.path.partition("?")[2].partition("v=")[2])[:10]
        if nome.startswith("img/"):
            cache = "public, max-age=604800"
        elif arq.suffix in (".css", ".js"):
            cache = CACHE_IMUTAVEL if v and v == versao(arq) else "no-cache"
        elif arq.suffix == ".html":
            cache = CACHE_PAGINA
        else:
            cache = CACHE_ARQUIVO
        extra = {"Cache-Control": cache}
        corpo = arq.read_bytes()
        if arq.suffix == ".html":
            extra["Content-Security-Policy"] = CSP_PAGINA
            corpo = carimbar(corpo)
        self._enviar(200, corpo, TIPOS.get(arq.suffix, "application/octet-stream"), extra)

    def _repassar(self):
        caminho, _, consulta = self.path.partition("?")
        api = caminho[len("/api"):]
        if not permitida("GET" if self.command == "HEAD" else self.command, api):
            return self._json(404, {"erro": "rota fora da API pública", "rota": api})
        if not TETO.aceita(self._ip()):
            return self._json(429, {"erro": f"mais de {TETO.por_minuto} pedidos por minuto deste IP; espere um minuto"})
        corpo = None
        if self.command == "POST":
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return self._json(400, {"erro": "Content-Length inválido"})
            if n > CORPO_MAX:
                return self._json(413, {"erro": f"corpo acima de {CORPO_MAX // 1024} KB"})
            corpo = self.rfile.read(n)
            chave = MEMORIA.chave(api + "?" + consulta, corpo)
            guardado = MEMORIA.ler(chave)
            if guardado is not None:
                return self._enviar(200, guardado[0], guardado[1], {"Cache-Control": "no-store", "X-Cache": "HIT",
                                                                    "Access-Control-Allow-Origin": "*"})
        req = urllib.request.Request(BACKEND + "/api" + api + (("?" + consulta) if consulta else ""),
                                     data=corpo, method=self.command)
        for h in ("Content-Type", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req, timeout=TEMPO_BACKEND) as r:
                status, dados, tipo = r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            status, dados, tipo = e.code, e.read(), e.headers.get("Content-Type", "application/json")
        except (urllib.error.URLError, OSError):
            return self._json(502, {"erro": "o motor de cálculo não respondeu; tente de novo em instantes"})
        if self.command == "POST" and status == 200:
            MEMORIA.guardar(chave, dados, tipo)
        publico = self.command in ("GET", "HEAD") and status == 200
        extra = {"Cache-Control": CACHE_API_GET if publico else "no-store", "Access-Control-Allow-Origin": "*"}
        if self.command == "POST":
            extra["X-Cache"] = "MISS"
        self._enviar(status, dados, tipo, extra)

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST",
                     "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "86400",
                     "Content-Length": "0"}.items():
            self.send_header(k, v)
        self.end_headers()


def subir_motor() -> subprocess.Popen | None:
    """Sobe o jar oficial. RTC_JAR vazio = motor externo (testes, dev)."""
    jar = os.environ.get("RTC_JAR", "")
    if not jar:
        return None
    flags = os.environ.get("RTC_JAVA_OPTS", "").split()
    return subprocess.Popen(["java", *flags, "-jar", jar, "--spring.profiles.active=offline",
                             "--server.port=8081", "--server.address=127.0.0.1"], cwd=os.environ.get("RTC_DIR", "/rtc"))


def subir_danfse() -> subprocess.Popen | None:
    """DANFSe Service da Receita (Go) em 127.0.0.1:8093, se a imagem o trouxer (RTC_DANFSE)."""
    binario = os.environ.get("RTC_DANFSE", "")
    if not binario:
        return None
    env = {**os.environ, "SERVER_PORT": "8093", "OTEL_SDK_DISABLED": "true"}
    os.environ.setdefault("DANFSE_URL", "http://127.0.0.1:8093")
    return subprocess.Popen([binario], env=env)


def vigiar(motor: subprocess.Popen, servidor: ThreadingHTTPServer) -> None:
    motor.wait()
    sys.stderr.write(f"motor saiu com código {motor.returncode}; encerrando para o pod reiniciar\n")
    servidor.shutdown()


def main() -> int:
    global CONTA, USO
    danfse = subir_danfse()
    CONTA = montar_conta()
    USO = montar_uso()
    motor = subir_motor()
    servidor = ServidorFila(("0.0.0.0", PORTA), Porta)
    servidor.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=servidor.shutdown).start())
    if motor:
        threading.Thread(target=vigiar, args=(motor, servidor), daemon=True).start()
    sys.stderr.write(f"nota-aberta em :{PORTA}, motor em {BACKEND}\n")
    servidor.serve_forever()
    if USO is not None:
        USO.despejar()
    if danfse and danfse.poll() is None:
        danfse.terminate()
    if motor and motor.poll() is None:
        motor.terminate()
        try:
            motor.wait(10)
        except subprocess.TimeoutExpired:
            motor.kill()
    return 1 if motor and motor.returncode not in (None, 0, 143) else 0


if __name__ == "__main__":
    sys.exit(main())
