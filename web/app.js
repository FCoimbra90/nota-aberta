// Página da nota-aberta. Toda conta é do motor oficial (POST /api/calculadora/observabilidade/regime-geral);
// aqui só se monta a entrada e se mostra a saída. Nenhum valor de tributo é calculado no navegador.
"use strict";

const API = "/api/calculadora";
const $ = (s, el = document) => el.querySelector(s);
const brl = (v) => Number(v).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const pct = (v) => Number(v).toLocaleString("pt-BR", { maximumFractionDigits: 4 }) + "%";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let situacoes = [];      // [{codigo, descricao, classificacoesTributarias:[{codigo, descricao}]}]
let classes = new Map(); // cClassTrib -> ficha completa (nomenclatura, reduções)

async function obter(caminho) {
  const r = await fetch(API + caminho);
  if (!r.ok) throw Object.assign(new Error("HTTP " + r.status), { status: r.status });
  return r.json();
}

function hoje() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

async function carregarVersao() {
  try {
    const v = await obter("/dados-abertos/versao");
    $("#versao").textContent = `Motor oficial ${v.versaoApp} · tabelas ${v.versaoDb} de ${v.dataVersaoDb.split("-").reverse().join("/")}`;
  } catch { $("#versao").textContent = "Motor oficial da Receita Federal"; }
}

async function carregarUfs() {
  const ufs = await obter("/dados-abertos/ufs");
  const sel = $("#uf");
  sel.innerHTML = '<option value="">Escolha</option>' + ufs.sort((a, b) => a.sigla.localeCompare(b.sigla))
    .map((u) => `<option value="${esc(u.sigla)}" data-cod="${u.codigo}">${esc(u.sigla)} · ${esc(u.nome)}</option>`).join("");
}

async function carregarMunicipios(manter) {
  const uf = $("#uf").value, sel = $("#municipio");
  sel.disabled = true;
  if (!uf) { sel.innerHTML = '<option value="">Escolha a UF</option>'; return; }
  sel.innerHTML = "<option>Carregando…</option>";
  const ms = await obter("/dados-abertos/ufs/municipios?siglaUf=" + encodeURIComponent(uf));
  sel.innerHTML = '<option value="">Escolha</option>' + ms.map((m) => `<option value="${m.codigo}">${esc(m.nome)}</option>`).join("");
  sel.disabled = false;
  if (manter) sel.value = String(manter);
}

// Até 31/12/2026 (período de teste) o motor resolve as alíquotas e recusa as informadas (REG-036);
// a partir de 2027 a alíquota nominal é obrigatória na entrada.
function informaAliquotas() { return ($("#data").value || "") >= "2027-01-01"; }

function travarAliquotas() {
  const livre = informaAliquotas();
  ["#a-cbs", "#a-uf", "#a-mun"].forEach((s) => { $(s).disabled = !livre; $(s).required = livre; });
}

async function aliquota(caminho) {
  try { return (await obter(caminho)).aliquotaReferencia; } catch { return null; }
}

async function carregarAliquotas() {
  const data = $("#data").value, opt = $("#uf").selectedOptions[0], mun = $("#municipio").value;
  if (!data) return;
  const ref = informaAliquotas() ? null : data;  // só há referência publicada para 2026
  if (!ref) { travarAliquotas(); }
  const [cbs, uf, m] = !ref ? [null, null, null] : await Promise.all([
    aliquota("/dados-abertos/aliquota-uniao?data=" + data),
    opt && opt.dataset.cod ? aliquota(`/dados-abertos/aliquota-uf?codigoUf=${opt.dataset.cod}&data=${data}`) : null,
    mun ? aliquota(`/dados-abertos/aliquota-municipio?codigoMunicipio=${mun}&data=${data}`) : null,
  ]);
  if (cbs != null) $("#a-cbs").value = cbs;
  if (uf != null) $("#a-uf").value = uf;
  if (m != null) $("#a-mun").value = m;
  travarAliquotas();
  $("#dica-aliq").textContent = !informaAliquotas()
    ? "Até 31/12/2026 o motor usa as alíquotas de referência do período de teste (mostradas aqui) e não aceita outras."
    : cbs == null
      ? "A partir de 2027 a alíquota nominal é informada por quem emite, e ainda não há referência publicada para esta data. Informe as alíquotas."
      : "Preenchidas com as alíquotas de referência da data, UF e município. Você pode trocar.";
}

async function carregarTabelas() {
  const data = $("#data").value;
  if (!data) return;
  const [sits, cls] = await Promise.all([
    obter("/dados-abertos/situacoes-tributarias/cbs-ibs?data=" + data),
    obter("/dados-abertos/classificacoes-tributarias/cbs-ibs?data=" + data),
  ]);
  situacoes = sits;
  classes = new Map(cls.map((c) => [c.codigo, c]));
  document.querySelectorAll(".item").forEach((it) => preencherCst(it, $(".cst", it).value, $(".cclass", it).value));
}

function preencherCst(it, cst, cclass) {
  const sel = $(".cst", it);
  sel.innerHTML = situacoes.map((s) => `<option value="${esc(s.codigo)}">${esc(s.codigo)} · ${esc(s.descricao)}</option>`).join("");
  if (cst && situacoes.some((s) => s.codigo === cst)) sel.value = cst;
  preencherClasse(it, cclass);
}

function preencherClasse(it, cclass) {
  const s = situacoes.find((x) => x.codigo === $(".cst", it).value);
  const sel = $(".cclass", it);
  const lista = s ? s.classificacoesTributarias : [];
  sel.innerHTML = lista.map((c) => `<option value="${esc(c.codigo)}">${esc(c.codigo)} · ${esc(c.descricao)}</option>`).join("");
  if (cclass && lista.some((c) => c.codigo === cclass)) sel.value = cclass;
  mostrarClasse(it);
}

function mostrarClasse(it) {
  const c = classes.get($(".cclass", it).value);
  const p = $(".info-class", it);
  if (!c) { p.textContent = ""; return; }
  const red = [c.percentualReducaoCbs, c.percentualReducaoIbsUf, c.percentualReducaoIbsMun];
  const nom = c.nomenclatura || "";
  if (nom === "NBS") $(".tipo", it).value = "nbs";
  else if (nom === "NCM") $(".tipo", it).value = "ncm";
  p.textContent = `${c.descricaoTratamentoTributario || c.tipoAliquota || ""}. Código exigido: ${nom || "—"}.` +
    (red.some((r) => r) ? ` Redução: CBS ${pct(red[0])}, IBS UF ${pct(red[1])}, IBS mun. ${pct(red[2])}.` : "");
}

function novoItem(v = {}) {
  const it = $("#tpl-item").content.firstElementChild.cloneNode(true);
  $("#itens").appendChild(it);
  $(".cst", it).addEventListener("change", () => preencherClasse(it));
  $(".cclass", it).addEventListener("change", () => mostrarClasse(it));
  $(".remover", it).addEventListener("click", () => { it.remove(); numerar(); });
  if (v.tipo) $(".tipo", it).value = v.tipo;
  if (v.codigo) $(".codigo", it).value = v.codigo;
  if (v.base != null) $(".base", it).value = v.base;
  if (v.qtd != null) $(".qtd", it).value = v.qtd;
  if (v.un) $(".un", it).value = v.un;
  preencherCst(it, v.cst || "000", v.cclass || "000001");
  numerar();
  return it;
}

function numerar() {
  document.querySelectorAll(".item").forEach((it, i) => { $(".n", it).textContent = i + 1; });
  document.querySelectorAll(".item .remover").forEach((b) => { b.hidden = document.querySelectorAll(".item").length < 2; });
}

function montarOperacao() {
  const num = (s) => Number(String(s).replace(",", "."));
  const itens = [...document.querySelectorAll(".item")].map((it, i) => {
    const item = { numero: i + 1, cst: $(".cst", it).value, cClassTrib: $(".cclass", it).value,
      baseCalculo: num($(".base", it).value) };
    const cod = $(".codigo", it).value.replace(/\D/g, "");
    if (cod) item[$(".tipo", it).value] = cod;
    const q = num($(".qtd", it).value);
    if (q > 0) { item.quantidade = q; item.unidade = ($(".un", it).value || "UN").toUpperCase(); }
    // Antes de 2027 o motor recusa alíquota informada (REG-036): ele mesmo resolve as vigentes.
    if (informaAliquotas()) item.aliquotasNominais = { cbs: num($("#a-cbs").value), ibsEstadual: num($("#a-uf").value), ibsMunicipal: num($("#a-mun").value) };
    return item;
  });
  return {
    id: "web-" + Date.now(), versao: "1.0.0",
    dhFatoGerador: $("#data").value + "T12:00:00-03:00",
    municipio: Number($("#municipio").value), uf: $("#uf").value, itens,
  };
}

function linhaTributo(nome, g, pChave, vChave) {
  if (!g) return "";
  const red = g.gRed ? `${pct(g.gRed.pRedAliq)} → ${pct(g.gRed.pAliqEfet)}` : "—";
  return `<tr><th scope="row">${nome}</th><td>${pct(g[pChave])}</td><td>${red}</td><td class="num">${brl(g[vChave])}</td></tr>`;
}

const ESTADOS = {
  CALCULADO: "Calculado", CALCULO_SIMULADO: "Cálculo simulado (período de teste)",
  INCONSISTENCIA_ENTRADA: "Entrada inconsistente", NAO_IMPLEMENTADO: "Ainda não implementado no motor", FALHA_TECNICA: "Falha técnica",
};

function mostrar(res, operacao) {
  const out = $("#resultado");
  const objs = res.objetos || [];
  let html = "<h2>Resultado</h2>";
  const tot = res.total && res.total.tribCalc && res.total.tribCalc.IBSCBSTot;
  if (tot) {
    const cbs = Number(tot.gCBS.vCBS), ibs = Number(tot.gIBS.vIBS);
    html += `<div class="totais">
      <div><span>Base CBS/IBS</span><strong>${brl(tot.vBCIBSCBS)}</strong></div>
      <div><span>CBS</span><strong>${brl(cbs)}</strong></div>
      <div><span>IBS (UF + município)</span><strong>${brl(ibs)}</strong></div>
      <div class="destaque"><span>CBS + IBS</span><strong>${brl(cbs + ibs)}</strong></div></div>`;
  }
  for (const o of objs) {
    const g = o.tribCalc && o.tribCalc.IBSCBS && o.tribCalc.IBSCBS.gIBSCBS;
    html += `<article class="res-item"><h3>Item ${o.nObj} <span class="estado e-${esc(o.estadoItem)}">${esc(ESTADOS[o.estadoItem] || o.estadoItem)}</span></h3>`;
    if (g) {
      html += `<table><thead><tr><th>Tributo</th><th>Alíquota</th><th>Redução → efetiva</th><th class="num">Valor</th></tr></thead><tbody>
        ${linhaTributo("CBS", g.gCBS, "pCBS", "vCBS")}
        ${linhaTributo("IBS estadual", g.gIBSUF, "pIBSUF", "vIBSUF")}
        ${linhaTributo("IBS municipal", g.gIBSMun, "pIBSMun", "vIBSMun")}
        </tbody></table><p class="dica">Base de cálculo: ${brl(g.vBC)}</p>`;
      const mem = g.gCBS && g.gCBS.memoriaCalculo;
      if (mem) html += `<details><summary>Memória de cálculo (texto do motor)</summary><p>${esc(mem)}</p></details>`;
    }
    for (const e of o.erros || []) html += `<p class="erro"><strong>${esc(e.code)}</strong> ${esc(e.title)}: ${esc(e.detail)}</p>`;
    html += "</article>";
  }
  for (const e of res.erros || []) html += `<p class="erro"><strong>${esc(e.code || "")}</strong> ${esc(e.title || "")}: ${esc(e.detail || "")}</p>`;
  html += `<details><summary>JSON enviado e recebido</summary><pre>${esc(JSON.stringify(operacao, null, 2))}</pre><pre>${esc(JSON.stringify(res, null, 2))}</pre></details>`;
  out.innerHTML = html;
  out.hidden = false;
  out.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function calcular(ev) {
  ev.preventDefault();
  const form = $("#form");
  if (!form.reportValidity()) return;
  const operacao = montarOperacao();
  const btn = $("#calcular");
  btn.disabled = true; btn.textContent = "Calculando…";
  try {
    const r = await fetch(API + "/observabilidade/regime-geral", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(operacao) });
    const res = await r.json().catch(() => ({}));
    if (!r.ok && !res.objetos) {
      res.erros = res.erros || [{ code: "HTTP " + r.status, title: res.title || res.erro || "Erro", detail: res.detail || "" }];
    }
    mostrar(res, operacao);
  } catch (e) {
    mostrar({ erros: [{ code: "rede", title: "Sem resposta", detail: String(e.message) }] }, operacao);
  } finally { btn.disabled = false; btn.textContent = "Calcular"; }
}

async function exemplo() {
  // Item 3 do exemplo oficial (docs/exemplos.md do repositório da Receita): educação, redução de 60%.
  $("#data").value = "2027-03-10";
  $("#uf").value = "RS";
  await carregarMunicipios(4314902);
  await carregarTabelas();
  await carregarAliquotas();
  $("#a-cbs").value = 8.8; $("#a-uf").value = 0.05; $("#a-mun").value = 0.05;
  $("#itens").innerHTML = "";
  novoItem({ tipo: "nbs", codigo: "122011200", cst: "200", cclass: "200028", base: 1200, qtd: 0 });
  novoItem({ tipo: "ncm", codigo: "10063021", cst: "200", cclass: "200003", base: 100, qtd: 20, un: "KG" });
}

async function iniciar() {
  $("#host").textContent = location.host;
  $("#data").value = hoje();
  // Os ouvintes entram ANTES de qualquer await: quem escolhia a UF enquanto as tabelas ainda carregavam
  // (ou um robô rápido) ficava sem a lista de municípios para sempre (medido no ar em 18/11/2026).
  $("#uf").addEventListener("change", async () => { await carregarMunicipios(); carregarAliquotas(); });
  $("#municipio").addEventListener("change", carregarAliquotas);
  $("#data").addEventListener("change", async () => { await carregarTabelas(); carregarAliquotas(); });
  $("#mais").addEventListener("click", () => novoItem({ base: 0 }));
  $("#form").addEventListener("submit", calcular);
  $("#exemplo").addEventListener("click", exemplo);
  carregarVersao();
  await Promise.all([carregarUfs(), carregarTabelas()]);
  novoItem({ base: 1000 });
  await carregarAliquotas();
}

iniciar().catch((e) => { $("#versao").textContent = "O motor de cálculo não respondeu: " + e.message; });
