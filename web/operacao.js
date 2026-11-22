// Painel de uso: lê /operacao/stats.json a cada 5 s (só com a aba visível) e desenha com SVG, sem biblioteca.
(function () {
  var $ = function (s) { return document.querySelector(s); };
  var TZ = "America/Sao_Paulo", nf = new Intl.NumberFormat("pt-BR");
  var S = null, rTl = "m60", pausado = false, timer = null;
  function n(x) { return nf.format(Number(x || 0)); }
  function e(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function pct(a, b) { return b ? (100 * a / b).toFixed(1).replace(".", ",") + "%" : "–"; }
  function path(pts, w, h, pad, mx) {
    if (!pts.length) return "";
    var k = pts.length, dx = k > 1 ? (w - 2 * pad) / (k - 1) : 0;
    var P = pts.map(function (v, i) { return [pad + i * dx, h - pad - (h - 2 * pad) * v / mx]; });
    var d = "M" + P[0][0].toFixed(1) + " " + P[0][1].toFixed(1);
    for (var i = 1; i < P.length; i++) { var a = P[i - 1], b = P[i], cx = (a[0] + b[0]) / 2;
      d += " C" + cx.toFixed(1) + " " + a[1].toFixed(1) + " " + cx.toFixed(1) + " " + b[1].toFixed(1) + " " + b[0].toFixed(1) + " " + b[1].toFixed(1); }
    return d;
  }
  function spark(pts) {
    var w = 200, h = 46, mx = Math.max.apply(null, pts.concat([1])), d = path(pts, w, h, 3, mx);
    if (!d) return "";
    return '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true"><path d="' + d + " L" + (w - 3) + " " + (h - 3) + " L3 " + (h - 3) + ' Z" fill="var(--vs)"/><path d="' + d + '" fill="none" stroke="var(--v)" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>';
  }
  function kpis() {
    var h = S.hoje, tl = (S.timeline.m60 || []).map(function (x) { return x.total; });
    var K = [
      ["big", "Pedidos por minuto (média de 5 min)", n(S.rpm), "agora", spark(tl)],
      ["", "Pedidos hoje", n(h.total), "desde 00:00 (Brasília)"],
      ["", "Visitantes únicos hoje", n(S.visitantes.hoje), "pessoas, pelo hash do dia"],
      ["", "Calculadora hoje", n(h.calculadora), "cálculos no motor oficial"],
      ["", "Agentes de IA hoje", n(h.agente), pct(h.agente, h.total) + " do total"],
      ["", "Latência média", n(h.lat_media_ms) + " ms", "hoje"],
      ["", "Erros 5xx", pct(h.s5, h.total), n(h.s5) + " hoje"],
      ["", "Emissões hoje", n(h.emissao), "pedidos de NFS-e e NF-e"],
    ];
    $("#kpis").innerHTML = K.map(function (k) { return '<div class="kpi ' + k[0] + '"><p class="r">' + e(k[1]) + '</p><p class="v">' + e(k[2]) + '</p><p class="s">' + e(k[3]) + "</p>" + (k[4] || "") + "</div>"; }).join("");
  }
  function rotulo(b) { return rTl === "m60" ? b.slice(11, 16) : rTl === "h24" ? b.slice(11, 16) : b.slice(8, 10) + "/" + b.slice(5, 7); }
  function chart() {
    var a = S.timeline[rTl] || [], w = 720, h = 200, pad = 24;
    $("#tl-sub").textContent = rTl === "m60" ? "por minuto, última hora" : rTl === "h24" ? "por hora, últimas 24 h" : "por dia, últimos 7 dias";
    if (!a.length) { $("#chart").innerHTML = '<p class="vazio">Sem dados ainda.</p>'; return; }
    var tot = a.map(function (x) { return x.total; }), pes = a.map(function (x) { return x.pessoa; });
    var mx = Math.max.apply(null, tot.concat([1])), k = a.length, step = Math.max(1, Math.ceil(k / 6)), ax = "";
    for (var i = 0; i < k; i += step) { var x = pad + (k > 1 ? i * (w - 2 * pad) / (k - 1) : 0);
      ax += '<text class="ax" x="' + x.toFixed(0) + '" y="' + (h - 5) + '" text-anchor="middle">' + e(rotulo(a[i].bucket)) + "</text>"; }
    var d = path(tot, w, h, pad, mx), dp = path(pes, w, h, pad, mx);
    var resumo = "Gráfico: " + k + " pontos, máximo de " + n(mx) + " em " + rotulo(a[tot.indexOf(mx)].bucket) + ", último " + n(tot[k - 1]) + ".";
    $("#chart").innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="' + e(resumo) + '"><defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--v)" stop-opacity=".25"/><stop offset="1" stop-color="var(--v)" stop-opacity="0"/></linearGradient></defs>' +
      '<text class="ax" x="' + pad + '" y="13">máx. ' + e(n(mx)) + "</text>" +
      '<path d="' + d + " L" + (w - pad) + " " + (h - pad) + " L" + pad + " " + (h - pad) + ' Z" fill="url(#g)"/>' +
      '<path d="' + d + '" fill="none" stroke="var(--v)" stroke-width="2.5" stroke-linecap="round"/>' +
      '<path d="' + dp + '" fill="none" stroke="var(--p)" stroke-width="2" stroke-linecap="round" stroke-dasharray="5 4"/>' + ax + "</svg>";
  }
  function bars(id, arr) {
    arr = arr.filter(function (x) { return x[1] > 0; });
    if (!arr.length) { $(id).innerHTML = '<li class="vazio">Nada hoje ainda.</li>'; return; }
    var mx = Math.max.apply(null, arr.map(function (x) { return x[1]; }));
    $(id).innerHTML = arr.map(function (x) { return "<li><span>" + e(x[0]) + '</span><span class="tr" aria-hidden="true"><i data-w="' + Math.max(2, Math.round(100 * x[1] / mx)) + '"></i></span><b>' + n(x[1]) + "</b></li>"; }).join("");
    // Largura pelo CSSOM: a CSP da página (style-src 'self') barra o atributo style escrito no HTML.
    $(id).querySelectorAll("[data-w]").forEach(function (i) { i.style.width = i.dataset.w + "%"; });
  }
  function canais() {
    var h = S.hoje;
    bars("#canais", [["Páginas", h.pagina], ["Calculadora", h.calculadora], ["Conta e painel", h.conta], ["Emissões", h.emissao], ["MCP", h.mcp], ["Arquivos (CSS, fotos)", h.estatico], ["Outros", h.outro]].sort(function (a, b) { return b[1] - a[1]; }));
    bars("#uas", [["Pessoas", h.pessoa], ["Agentes de IA", h.agente], ["Robôs", h.robo]]);
    $("#st").textContent = "Respostas hoje: " + n(h.s2) + " OK · " + n(h.s3) + " redirecionadas · " + n(h.s4) + " recusadas (4xx) · " + n(h.s5) + " erro (5xx)";
  }
  function tabela(id, arr, cab, fmt) {
    $(id).innerHTML = arr && arr.length ? '<table class="tb"><thead><tr><th scope="col">' + e(cab) + '</th><th scope="col">Hoje</th></tr></thead><tbody>' +
      arr.map(function (x) { return "<tr><td>" + (fmt ? fmt(x.chave) : e(x.chave)) + "</td><td>" + n(x.n) + "</td></tr>"; }).join("") + "</tbody></table>" : '<p class="vazio">Nada acima do mínimo ainda.</p>';
  }
  function pais(c) { try { return e(new Intl.DisplayNames(["pt-BR"], { type: "region" }).of(c) || c); } catch (x) { return e(c); } }
  function soma(o) { var t = 0; for (var k in o) t += o[k]; return t; }
  function negocio() {
    var g = S.negocio;
    if (!g) { $("#negocio").innerHTML = '<p class="vazio">Banco das contas desligado neste servidor.</p>'; return; }
    if (g.erro) { $("#negocio").innerHTML = '<p class="vazio">' + e(g.erro) + "</p>"; return; }
    var aut = function (o) { return (o.producao.emitida || 0); };
    var M = [["Contas", g.contas, "+" + n(g.contas_hoje) + " hoje"], ["Empresas", g.empresas, n(g.certificados) + " com certificado"],
      ["NFS-e em produção", aut(g.nfse), n(soma(g.nfse.producao)) + " tentativas · " + n(soma(g.nfse.teste)) + " de teste"],
      ["NF-e em produção", aut(g.nfe), n(soma(g.nfe.producao)) + " tentativas · " + n(soma(g.nfe.teste)) + " de teste"],
      ["Notas hoje", g.nfse.hoje + g.nfe.hoje, "NFS-e e NF-e, todos os ambientes"], ["Chaves de API", g.chaves_api, "para sistemas e agentes"]];
    $("#negocio").innerHTML = M.map(function (m) { return "<div><small>" + e(m[0]) + "</small><b>" + n(m[1]) + "</b><small>" + e(m[2]) + "</small></div>"; }).join("");
  }
  var UF = { 11: "RO", 12: "AC", 13: "AM", 14: "RR", 15: "PA", 16: "AP", 17: "TO", 21: "MA", 22: "PI", 23: "CE", 24: "RN", 25: "PB",
    26: "PE", 27: "AL", 28: "SE", 29: "BA", 31: "MG", 32: "ES", 33: "RJ", 35: "SP", 41: "PR", 42: "SC", 43: "RS", 50: "MS", 51: "MT",
    52: "GO", 53: "DF" };
  var REGIME = { simples: "Simples", mei: "MEI", normal: "Lucro Real/Presumido" };
  function funil() {
    var g = S.negocio, f = g && g.funil;
    if (!f) { $("#funil").innerHTML = ""; return; }
    var P = [["Criaram conta", f.contas], ["Cadastraram empresa", f.com_empresa], ["Subiram o certificado", f.com_certificado],
      ["Emitiram alguma nota", f.emitiu], ["Emitiram com valor fiscal", f.emitiu_producao]];
    $("#funil").innerHTML = P.map(function (p, i) {
      var pct = f.contas ? Math.round(100 * p[1] / f.contas) : 0;
      var queda = i && P[i - 1][1] ? Math.round(100 * p[1] / P[i - 1][1]) + "% da etapa anterior" : "";
      return '<li><span class="fn-n">' + n(p[1]) + '</span><span class="fn-t">' + e(p[0]) + '<small>' + e(queda) + '</small></span>' +
        '<span class="fn-b"><i data-w="' + pct + '"></i></span></li>';
    }).join("");
    $("#funil").querySelectorAll("[data-w]").forEach(function (i) { i.style.width = Math.max(1, i.dataset.w) + "%"; });
  }
  function empresas() {
    var L = (S.negocio && S.negocio.lista) || [];
    if (!L.length) { $("#empresas").innerHTML = '<p class="vazio">Nenhuma empresa cadastrada ainda.</p>'; return; }
    $("#empresas").innerHTML = '<table class="tb"><thead><tr><th scope="col">Empresa</th><th scope="col">Onde</th><th scope="col">Etapa</th>' +
      '<th scope="col">Valor fiscal</th><th scope="col">Teste</th><th scope="col">Recusadas</th><th scope="col">Último erro</th></tr></thead><tbody>' +
      L.map(function (x) {
        var uf = UF[String(x.municipio_ibge).slice(0, 2)] || "";
        var err = x.ultimo_erro ? "<code>" + e(x.ultimo_erro.codigo) + "</code> " + e(x.ultimo_erro.mensagem) +
          (x.ultimo_erro.dica ? "<small>" + e(x.ultimo_erro.dica) + "</small>" : "") : "";
        return "<tr><td>" + e(x.razao) + "<small>" + e(REGIME[x.regime] || x.regime) + " · desde " + e(x.criada.split("-").reverse().join("/")) +
          "</small></td><td>" + e(uf) + "<small>IBGE " + e(x.municipio_ibge) + "</small></td><td>" + e(x.etapa) +
          (x.certificado ? "" : "<small>sem certificado</small>") + "</td><td>" + n(x.nfse_producao) + "</td><td>" + n(x.nfse_teste) +
          "</td><td>" + n(x.recusadas) + "</td><td class=\"erro\">" + err + "</td></tr>";
      }).join("") + "</tbody></table>";
  }
  function extras() {
    tabela("#origem", S.top.origem, "Origem");
    tabela("#paginas", S.top.pagina, "Página");
    tabela("#paises", S.top.pais, "País", pais);
    tabela("#tools", S.top.ferramenta, "Ferramenta", function (k) { return "<code>" + e(k) + "</code>"; });
  }
  function ago() {
    if (!S) return;
    $("#ago").textContent = (pausado ? "Pausado. " : "") + "Atualizado às " + new Date(S.gerado_em).toLocaleTimeString("pt-BR", { timeZone: TZ }) + " (Brasília).";
  }
  function load() {
    fetch("/operacao/stats.json", { cache: "no-store" }).then(function (r) {
      if (r.status === 403) { location.reload(); throw new Error("sem acesso"); }
      return r.json();
    }).then(function (d) { S = d; kpis(); chart(); canais(); negocio(); funil(); empresas(); extras(); ago(); })
      .catch(function () { $("#ago").textContent = "Sem conexão com o painel agora; tentando de novo."; });
  }
  function tick() { clearInterval(timer); if (!pausado) timer = setInterval(function () { if (!document.hidden) load(); }, 5000); }
  var abas = document.querySelectorAll("#tabs-tl [role=tab]");
  abas.forEach(function (b, i) {
    function sel(x) { abas.forEach(function (y) { var on = y === x; y.setAttribute("aria-selected", on ? "true" : "false"); y.tabIndex = on ? 0 : -1; }); rTl = x.dataset.r; if (S) chart(); }
    b.addEventListener("click", function () { sel(b); });
    b.addEventListener("keydown", function (ev) {
      var j = ev.key === "ArrowRight" ? (i + 1) % abas.length : ev.key === "ArrowLeft" ? (i - 1 + abas.length) % abas.length : -1;
      if (j < 0) return; ev.preventDefault(); abas[j].focus(); sel(abas[j]);
    });
  });
  $("#pausa").addEventListener("click", function () {
    pausado = !pausado; this.setAttribute("aria-pressed", pausado ? "true" : "false");
    document.body.classList.toggle("pausado", pausado);
    $("#pausa-t").textContent = pausado ? "Pausado · continuar" : "Ao vivo · pausar"; ago(); tick(); if (!pausado) load();
  });
  load(); tick();
})();
