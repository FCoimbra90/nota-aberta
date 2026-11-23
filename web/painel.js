// Painel da conta. Toda escrita leva o cabeçalho X-Nota (o servidor recusa sem ele: CSRF).
// O certificado vai em base64 direto para /conta/empresas/<id>/certificado e não fica em nenhum lugar do navegador.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const brl = (v) => Number(v).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const ESTADOS = { emitida: "Emitida", recusada: "Recusada", desconhecida: "Aguardando o governo", pendente: "Pendente", cancelada: "Cancelada" };

async function api(metodo, caminho, corpo) {
  const r = await fetch(caminho, { method: metodo, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Nota": "1" }, body: corpo ? JSON.stringify(corpo) : undefined });
  const dados = await r.json().catch(() => ({}));
  if (!r.ok && r.status !== 202) throw Object.assign(new Error(dados?.erro?.mensagem || `erro ${r.status}`), { status: r.status });
  return dados;
}

let EU = null;

function plano(eu, planos) {
  const p = eu.plano;
  const usadas = eu.empresas.reduce((s, e) => s + e.notas_no_mes, 0);
  const outros = planos.filter((x) => x.checkout && x.preco_mes > p.preco_mes);  // sobe de plano por empresas, não por notas
  const reais = (v) => Number(v).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  $("#plano").innerHTML = `<h2>Plano ${esc(p.nome)}</h2>
    <p>${usadas} nota(s) neste mês${p.notas_mes == null ? " · notas ilimitadas" : ` de ${p.notas_mes}`} · ${p.empresas == null ? "empresas ilimitadas" : `até ${p.empresas} empresa(s)`}</p>
    <div class="acoes">${outros.map((o) => `<a class="botao" href="${esc(o.checkout)}">${esc(o.nome)}: R$ ${reais(o.preco_mes)}/mês${o.teste_dias ? `, ${o.teste_dias} dias grátis` : ""}</a>`).join("")}
    ${outros.length ? '<button type="button" class="secundario" id="ja-assinei">Já assinei: atualizar</button>' : ""}</div>
    ${p.whatsapp ? `<p><a class="botao" href="https://wa.me/${encodeURIComponent(p.whatsapp)}?text=${encodeURIComponent("Sou assinante do Suporte da Nota Aberta")}" rel="noopener">Falar com o suporte no WhatsApp</a></p>` : ""}
    <p class="dica">${outros.length ? "Assine com o mesmo e-mail do login: o plano muda sozinho." : "Planos pagos aparecem aqui quando a cobrança estiver ligada."}</p>`;
  const b = $("#ja-assinei");
  if (b) b.onclick = async () => { await api("POST", "/conta/plano/atualizar"); carregar(); };
}

function empresas(eu) {
  const caixa = $("#empresas");
  caixa.innerHTML = eu.empresas.length ? "" : '<p class="dica">Nenhuma empresa ainda. Adicione abaixo.</p>';
  for (const e of eu.empresas) {
    const el = $("#tpl-empresa").content.firstElementChild.cloneNode(true);
    $(".e-nome", el).textContent = `${e.razao} · ${e.cnpj}`;
    const c = e.certificado;
    $(".e-info", el).textContent = c ? `Certificado de ${c.titular}, válido até ${c.valido_ate.split("-").reverse().join("/")}. ${e.notas_no_mes} nota(s) no mês.`
      : "Sem certificado: suba o A1 para emitir.";
    $(".apagar-cert", el).hidden = !c;
    $(".apagar-cert", el).onclick = async () => {
      if (!confirm("Apagar o certificado desta empresa? Ele some do cofre e as notas param até subir outro.")) return;
      await api("DELETE", `/conta/empresas/${e.id}/certificado`); carregar();
    };
    $(".f-cert", el).onsubmit = async (ev) => {
      ev.preventDefault();
      const f = ev.target, saida = $(".r-cert", el);
      const arquivo = f.pfx.files[0];
      if (!arquivo || arquivo.size > 64 * 1024) { saida.textContent = "Escolha o arquivo .pfx (até 64 KB)."; return; }
      const bytes = new Uint8Array(await arquivo.arrayBuffer());
      let bin = "";
      for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
      const b64 = btoa(bin);
      try {
        const r = await api("POST", `/conta/empresas/${e.id}/certificado`, { pfx_base64: b64, senha: f.senha.value });
        f.reset(); saida.textContent = `Guardado, válido até ${r.valido_ate.split("-").reverse().join("/")}.`; carregar();
      } catch (err) { saida.textContent = err.message; }
    };
    const ff = $(".f-fiscal", el);
    ff.inscricao_municipal.value = e.inscricao_municipal || "";
    ff.regime_especial.value = e.regime_especial || "0";
    ff.onsubmit = async (ev) => {
      ev.preventDefault();
      const saida = $(".r-fiscal", el);
      try {
        const r = await api("PATCH", `/conta/empresas/${e.id}`,
          { inscricao_municipal: ff.inscricao_municipal.value.trim(), regime_especial: ff.regime_especial.value });
        ff.inscricao_municipal.value = r.inscricao_municipal; saida.textContent = "Salvo: vale para as próximas notas.";
      } catch (err) { saida.textContent = err.message; }
    };
    caixa.appendChild(el);
  }
  const sel = $("#f-emitir").empresa, atual = sel.value;
  sel.innerHTML = eu.empresas.map((e) => `<option value="${esc(e.id)}" data-regime="${esc(e.regime)}">${esc(e.razao)}</option>`).join("");
  if (atual) sel.value = atual;
  campoSimples();
  if (sel.value) notas(sel.value);
}

async function notas(eid) {
  const { notas: lista } = await api("GET", `/conta/empresas/${eid}/notas`);
  $("#notas").innerHTML = lista.length ? `<table><thead><tr><th>Quando</th><th>Tomador</th><th class="num">Valor</th><th>Situação</th></tr></thead><tbody>
    ${lista.map((n) => `<tr><td>${esc(n.criada.slice(0, 16).replace("T", " "))}</td><td>${esc(n.tomador || "—")}</td><td class="num">${brl(n.valor)}</td>
      <td>${n.producao ? "" : '<span class="etiqueta">teste</span> '}${esc(ESTADOS[n.status] || n.status)}${n.numero_nfse ? ` nº ${esc(n.numero_nfse)}` : ""}${n.status === "emitida" ? ` · <a href="/conta/empresas/${esc(eid)}/notas/${esc(n.id)}/pdf" target="_blank" rel="noopener">PDF</a> · <button type="button" class="secundario mini cancelar-nota" data-n="${esc(n.id)}" data-num="${esc(n.numero_nfse || "")}">Cancelar</button>` : ""}${["emitida", "recusada", "cancelada"].includes(n.status) ? ` · <button type="button" class="secundario mini repetir-nota" data-n="${esc(n.id)}">Repetir</button>` : ""}${errosHtml(n)}${tributosHtml(n)}</td></tr>`).join("")}
    </tbody></table>` : '<p class="dica">Nenhuma nota ainda.</p>';
  document.querySelectorAll("#notas .cancelar-nota").forEach((b) => { b.onclick = () => cancelar(eid, b.dataset.n, b.dataset.num); });
  document.querySelectorAll("#notas .repetir-nota").forEach((b) => { b.onclick = () => repetir(lista.find((x) => x.id === b.dataset.n)); });
}

// O que a Sefin calculou na nota (não o que pedimos): CBS e IBS da Reforma, retenções e líquido.
// Erro da nota em linguagem de gente: o que aconteceu (mensagem já limpa pelo servidor) + o que fazer (dica), com o
// código do governo pequeno no fim, para o suporte. Só para recusada: em nota emitida o erro antigo não interessa.
function errosHtml(n) {
  if (!n.erros.length || n.status !== "recusada") return "";
  return `<div class="erro-nota" role="note">${n.erros.map((x) => `<p><strong>${esc(String(x.mensagem || "").replace(/^./, (c) => c.toUpperCase()))}</strong>${x.dica ? `<span>${esc(x.dica)}</span>` : ""}<small>${x.codigo === "entrada" ? "conferido aqui, antes de enviar" : `código do governo: ${esc(x.codigo)}`}</small></p>`).join("")}</div>`;
}
function tributosHtml(n) {
  const t = n.tributos || {}, r = (v) => brl(Number(v));
  const partes = [t.cbs && `CBS ${r(t.cbs)}`, t.ibs && `IBS ${r(t.ibs)}`, t.iss && `ISS ${r(t.iss)}`,
    t.retencoes && Number(t.retencoes) > 0 && `retido ${r(t.retencoes)}`, t.liquido && `líquido ${r(t.liquido)}`].filter(Boolean);
  return partes.length ? `<span class="tributos">${esc(partes.join(" · "))}</span>` : "";
}

// Contrato recorrente (licitação de prefeitura): mesma nota todo mês, muda só a competência. Preenche o formulário
// com a nota escolhida e deixa a competência para a pessoa conferir; nada é enviado sem clicar em Emitir.
function repetir(n) {
  const f = $("#f-emitir"), s = n.servico || {}, t = s.tomador || {};
  f.codigo_servico.value = s.codigo_servico || ""; f.descricao.value = s.descricao || ""; f.nbs.value = s.nbs || "";
  f.valor.value = Number(n.valor).toFixed(2).replace(".", ",");
  if (s.aliquota_simples && !$("#l-simples").hidden) f.aliquota_simples.value = s.aliquota_simples.replace(".", ",");
  f.doc.value = t.documento || ""; f.nome.value = t.nome || ""; f.email.value = t.email || "";
  for (const k of CAMPOS_END) f[k].value = t[k] || "";
  f.cidade.value = t.municipio_ibge ? `IBGE ${t.municipio_ibge}` : "";
  $("#end-tomador").open = Boolean(t.cep);
  const ret = s.retencoes || {};
  for (const k of CAMPOS_RET) f[k].value = ret[k] ? (ret[k] / 100).toFixed(2).replace(".", ",") : "";
  f.aliquota_iss.value = ret.aliquota_iss || ""; f.iss_retido.checked = Boolean(ret.iss_retido);
  f.pis_cofins_retidos.checked = Boolean(ret.pis_cofins_retidos);
  $("#retencoes").open = Boolean(s.retencoes);
  f.sem_rtc.checked = Boolean(s.nbs) && !n.ibscbs;
  consultarRtc();
  f.ambiente.value = n.producao ? "producao" : "teste"; marcarAmbiente();
  const d = $("#dica-repetir"); d.hidden = false;
  d.textContent = `Copiado da nota de ${n.criada.slice(0, 10).split("-").reverse().join("/")}. Confira a competência antes de emitir.`;
  f.scrollIntoView({ behavior: "smooth", block: "start" }); f.competencia.focus();
}

// Endereço do tomador: vai inteiro ou não vai (o servidor recusa pela metade). O CEP preenche rua, bairro e o código IBGE.
const CAMPOS_END = ["cep", "logradouro", "numero", "complemento", "bairro", "municipio_ibge"];
function enderecoDoForm(f) {
  const end = Object.fromEntries(CAMPOS_END.map((k) => [k, f[k].value.trim()]).filter(([, v]) => v));
  if (end.cep) end.cep = end.cep.replace(/\D/g, "");
  return end;
}
async function buscarCep(f) {
  const cep = f.cep.value.replace(/\D/g, ""), saida = $("#r-cep");
  if (cep.length !== 8) return;
  saida.textContent = "Buscando o CEP…";
  try {
    const d = await (await fetch(`https://viacep.com.br/ws/${cep}/json/`)).json();
    if (d.erro) { saida.textContent = "CEP não encontrado: preencha à mão."; return; }
    f.logradouro.value ||= d.logradouro || ""; f.bairro.value ||= d.bairro || "";
    f.municipio_ibge.value = d.ibge || ""; f.cidade.value = d.localidade ? `${d.localidade}/${d.uf}` : "";
    saida.textContent = "Endereço preenchido pelo CEP. Confira e coloque o número.";
    f.numero.focus();
  } catch { saida.textContent = "Não consegui consultar o CEP agora."; }
}

// Retenções na fonte: em reais no formulário; o servidor confere e monta tribFed e o ISS retido.
const CAMPOS_RET = ["irrf", "csll", "inss", "pis", "cofins"];
// "1.500,00" e "1500,00" (brasileiro) e "1500.00" (ponto decimal) valem o mesmo.
const num = (v) => { const t = String(v || "").trim(); return Number(t.includes(",") ? t.replace(/\./g, "").replace(",", ".") : t) || 0; };
function retencoesDoForm(f) {
  const r = Object.fromEntries(CAMPOS_RET.map((k) => [k, num(f[k].value).toFixed(2)]));
  Object.assign(r, { iss_retido: f.iss_retido.checked, aliquota_iss: f.aliquota_iss.value.replace(",", ".").trim(),
    pis_cofins_retidos: f.pis_cofins_retidos.checked });
  return r.iss_retido || r.aliquota_iss || CAMPOS_RET.some((k) => Number(r[k]) > 0) ? r : undefined;
}
function totalRetido(f) {
  const federais = CAMPOS_RET.filter((k) => !["pis", "cofins"].includes(k) || f.pis_cofins_retidos.checked)
    .reduce((s, k) => s + num(f[k].value), 0);
  const iss = f.iss_retido.checked ? num(f.valor.value) * num(f.aliquota_iss.value) / 100 : 0;
  $("#r-liquido").textContent = federais + iss > 0 ? `Retido informado: ${brl(federais + iss)}. O valor oficial de cada retenção é o que sai na nota.` : "";
}

// IBS/CBS da Reforma: o motor oficial da Receita (o mesmo jar da calculadora) classifica o NBS; quem calcula o valor
// exato é a Sefin, dentro da nota. A prévia usa a base de cálculo e as alíquotas de referência do mesmo motor.
let RTC = null;
const pegar = (u) => fetch(u).then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))));
function dataRtc(f) { return f.competencia.value || new Date().toISOString().slice(0, 10); }
async function consultarRtc() {
  const f = $("#f-emitir"), nbs = f.nbs.value.replace(/\D/g, ""), classe = $("#rtc-classe"), escolha = $("#rtc-escolha");
  RTC = null; escolha.hidden = true; $("#rtc-previa").textContent = "";
  $("#rtc").hidden = nbs.length !== 9;
  if (nbs.length !== 9) return;
  classe.textContent = "Consultando o motor oficial…";
  const d = dataRtc(f);
  try {
    const [cls, ind] = await Promise.all([
      pegar(`/api/calculadora/nfse/situacoes-classificacoes-tributarias?nbs=${nbs}&data=${d}`),
      pegar(`/api/calculadora/nfse/indicador-operacao?dataOcorrenciaFatoGerador=${d}&nbs=${nbs}`)]);
    RTC = { cls, ind };
  } catch { classe.textContent = "O motor oficial não respondeu agora. Tente de novo ou marque “Emitir sem o grupo IBS/CBS”."; return; }
  if (!RTC.cls.length || !RTC.ind.length) {
    classe.textContent = "O motor oficial não tem classificação de IBS/CBS para este NBS nesta data. Confira o NBS."; RTC = null; return;
  }
  const opcoes = [];
  for (const c of RTC.cls) for (const i of RTC.ind) opcoes.push({ c, i });
  RTC.opcoes = opcoes;
  if (opcoes.length > 1) {
    escolha.hidden = false;
    f.classe_rtc.innerHTML = opcoes.map((o, k) => `<option value="${k}">CST ${esc(o.c.codigoSituacaoTributaria)} · ${esc(o.c.codigoClassificacaoTributaria)} ${esc(o.c.descricaoClassificacaoTributaria)} · operação ${esc(o.i.cIndOp)}</option>`).join("");
  }
  mostrarClasse(); previa();
}
function opcaoRtc() { return RTC && RTC.opcoes[RTC.opcoes.length > 1 ? Number($("#f-emitir").classe_rtc.value) : 0]; }
function mostrarClasse() {
  const o = opcaoRtc();
  if (!o) return;
  $("#rtc-classe").textContent = `CST ${o.c.codigoSituacaoTributaria} · cClassTrib ${o.c.codigoClassificacaoTributaria}: ${o.c.descricaoClassificacaoTributaria} ` +
    `Operação ${o.i.cIndOp} (${o.i.tipoOperacao}); incide em: ${o.i.localIncidencia}.`;
}
async function aliquotaRef(caminho) {
  try { return (await pegar(`/api/calculadora/dados-abertos/${caminho}`)).aliquotaReferencia; } catch { return null; }
}
async function previa() {
  const f = $("#f-emitir"), saida = $("#rtc-previa"), valor = num(f.valor.value);
  if (!RTC || !valor || f.sem_rtc.checked) { saida.textContent = ""; return; }
  const d = dataRtc(f), emp = (EU?.empresas || []).find((e) => e.id === f.empresa.value);
  const mun = String(f.municipio_ibge.value || emp?.municipio_ibge || "");
  const iss = f.aliquota_iss.value ? valor * num(f.aliquota_iss.value) / 100 : 0;
  try {
    const r = await fetch("/api/calculadora/nfse/base-calculo", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ anoFatoGerador: Number(d.slice(0, 4)), valorServico: valor, iss, pis: num(f.pis.value), cofins: num(f.cofins.value) }) });
    const { baseCalculo } = await r.json();
    const [cbs, uf, munic] = await Promise.all([aliquotaRef(`aliquota-uniao?data=${d}`),
      mun ? aliquotaRef(`aliquota-uf?codigoUf=${mun.slice(0, 2)}&data=${d}`) : null,
      mun ? aliquotaRef(`aliquota-municipio?codigoMunicipio=${mun}&data=${d}`) : null]);
    const pct = (a) => `${Number(a).toLocaleString("pt-BR", { maximumFractionDigits: 2 })}%`;
    const ibs = (Number(uf) || 0) + (Number(munic) || 0);
    saida.textContent = `Prévia: base ${brl(baseCalculo)}` + (cbs != null ? ` · CBS ${pct(cbs)} ≈ ${brl(baseCalculo * cbs / 100)}` : "") +
      (uf != null ? ` · IBS ${pct(ibs)} ≈ ${brl(baseCalculo * ibs / 100)}` : "") + ". O valor oficial é o que a Sefin calcula na nota.";
  } catch { saida.textContent = ""; }
}

// Cancelamento é evento no governo e não tem volta: pede motivo (o governo exige 15+ caracteres) e confirma.
async function cancelar(eid, nid, numero) {
  const codigo = prompt("Motivo do cancelamento:\n1 = erro na emissão\n2 = serviço não prestado\n9 = outro", "1");
  if (codigo === null) return;
  if (!["1", "2", "9"].includes(codigo.trim())) { alert("Digite 1, 2 ou 9."); return; }
  const motivo = prompt("Descreva o motivo (15 caracteres ou mais):", "");
  if (motivo === null) return;
  if (motivo.trim().length < 15) { alert("O motivo precisa ter 15 caracteres ou mais."); return; }
  if (!confirm(`Cancelar a NFS-e${numero ? ` nº ${numero}` : ""}? O cancelamento vai para o governo e não tem volta.`)) return;
  try {
    await api("POST", `/conta/empresas/${eid}/notas/${nid}/cancelar`, { motivo_codigo: codigo.trim(), motivo: motivo.trim() });
    alert("Nota cancelada no governo.");
  } catch (err) { alert(`Não cancelou: ${err.message}`); }
  notas(eid);
}

function chaves(eu) {
  $("#chaves").innerHTML = eu.chaves.length ? `<ul class="lista">${eu.chaves.map((c) => `<li><code>${esc(c.prefixo)}…</code> ${esc(c.nome)} <button type="button" class="secundario mini" data-p="${esc(c.prefixo)}">Revogar</button></li>`).join("")}</ul>` : "";
  document.querySelectorAll("#chaves [data-p]").forEach((b) => { b.onclick = async () => { await api("DELETE", `/conta/chaves/${b.dataset.p}`); carregar(); }; });
}

async function carregar() {
  try { EU = await api("GET", "/conta/eu"); } catch (e) {
    if (e.status === 401) { $("#fora").hidden = false; $("#dentro").hidden = true; return; }
    $("#fora").hidden = false; $("#fora").querySelector("p").textContent = e.message; return;
  }
  $("#fora").hidden = true; $("#dentro").hidden = false;
  $("#quem").textContent = EU.usuario.email;
  const amb = $("#ambiente");
  amb.hidden = false;
  amb.textContent = EU.producao ? "Escolha em cada nota: Teste (sem valor fiscal) ou Valor fiscal (nota de verdade)."
    : "Este servidor só emite nota de teste (ambiente de teste do governo): as notas NÃO têm valor fiscal.";
  amb.classList.toggle("faixa-teste", !EU.producao);
  const fiscal = $('#f-emitir input[value="producao"]');
  fiscal.disabled = !EU.producao;
  if (!EU.producao) $('#f-emitir input[value="teste"]').checked = true;
  marcarAmbiente();
  empresas(EU); chaves(EU); nfeEmpresas(EU);
}

$("#f-emitir").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target, saida = $("#r-emitir");
  const tomador = f.doc.value.trim() ? { documento: f.doc.value, nome: f.nome.value, email: f.email.value, ...enderecoDoForm(f) } : undefined;
  saida.textContent = "Enviando ao governo…";
  try {
    const ambiente = f.ambiente.value;
    if (ambiente === "producao" && !confirm("Emitir NOTA COM VALOR FISCAL? Ela vale para a Receita e para a prefeitura.")) { saida.textContent = ""; return; }
    const n = await api("POST", `/conta/empresas/${f.empresa.value}/notas`, { codigo_servico: f.codigo_servico.value, descricao: f.descricao.value,
      valor: f.valor.value, tomador, ambiente, competencia: f.competencia.value, nbs: f.nbs.value.replace(/\D/g, ""),
      retencoes: retencoesDoForm(f), ibscbs: ibscbsDoForm(f), aliquota_simples: aliquotaSimplesDoForm(f), ref: `painel-${Date.now()}` });
    saida.textContent = n.status === "emitida" ? `Nota emitida${n.numero_nfse ? ` nº ${n.numero_nfse}` : ""}.`
      : n.status === "desconhecida" ? "O governo não respondeu. Ela aparece na lista assim que confirmar." : `Recusada: ${n.erros.map((x) => x.dica || x.mensagem).join("; ")}`;
    carregar();
  } catch (err) { saida.textContent = err.message; }
};
$("#f-emitir").empresa.onchange = (ev) => { campoSimples(); notas(ev.target.value); previa(); };
// Alíquota do Simples (pTotTribSN): só ME/EPP optante informa; MEI e regime normal o governo recusa (E0710 a E0713).
// A última usada fica lembrada neste navegador, por empresa, porque muda pouco de uma nota para outra.
const chaveSimples = (eid) => `na_simples_${eid}`;
function campoSimples() {
  const f = $("#f-emitir"), op = f.empresa.selectedOptions[0], simples = Boolean(op) && op.dataset.regime === "simples";
  $("#l-simples").hidden = !simples;
  if (!simples) { f.aliquota_simples.value = ""; return; }
  try { f.aliquota_simples.value = localStorage.getItem(chaveSimples(f.empresa.value)) || ""; } catch { /* sem armazenamento */ }
}
function aliquotaSimplesDoForm(f) {
  if ($("#l-simples").hidden) return undefined;
  const v = f.aliquota_simples.value.trim();
  try { localStorage.setItem(chaveSimples(f.empresa.value), v); } catch { /* sem armazenamento */ }
  return v || undefined;
}
// Sem escolha explícita o servidor pergunta ao mesmo motor; a escolha só vai quando o NBS tem mais de uma opção.
function ibscbsDoForm(f) {
  if (f.sem_rtc.checked) return false;
  const o = RTC && RTC.opcoes.length > 1 ? opcaoRtc() : null;
  return o ? { cst: o.c.codigoSituacaoTributaria, cclass_trib: o.c.codigoClassificacaoTributaria, cind_op: o.i.cIndOp } : undefined;
}
{
  const f = $("#f-emitir");
  f.nbs.addEventListener("change", consultarRtc);
  f.competencia.addEventListener("change", consultarRtc);
  f.classe_rtc.addEventListener("change", () => { mostrarClasse(); previa(); });
  f.sem_rtc.addEventListener("change", previa);
  for (const k of ["valor", "aliquota_iss", "pis", "cofins", "irrf", "csll", "inss"]) f[k].addEventListener("input", () => { previa(); totalRetido(f); });
  for (const k of ["iss_retido", "pis_cofins_retidos"]) f[k].addEventListener("change", () => totalRetido(f));
}
$("#f-emitir").cep.addEventListener("blur", () => buscarCep($("#f-emitir")));
// Competência começa em hoje (horário de Brasília pelo relógio da pessoa) e fica aberta: serviço de outubro emitido em novembro.
$("#f-emitir").competencia.value = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

// O botão diz o que vai sair: "nota de teste" ou "nota com valor fiscal" (vermelho), para ninguém emitir fiscal sem ver.
function marcarAmbiente() {
  const fiscal = $("#f-emitir").ambiente.value === "producao", b = $("#b-emitir");
  b.textContent = fiscal ? "Emitir nota com valor fiscal" : "Emitir nota de teste";
  b.classList.toggle("botao-fiscal", fiscal);
}
document.querySelectorAll('#f-emitir input[name="ambiente"]').forEach((r) => { r.onchange = marcarAmbiente; });

$("#f-empresa").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try {
    await api("POST", "/conta/empresas", { cnpj: f.cnpj.value, razao: f.razao.value, municipio_ibge: Number(f.municipio.value), regime: f.regime.value });
    f.reset(); $("#r-empresa").textContent = "Empresa adicionada. Agora suba o certificado."; carregar();
  } catch (err) { $("#r-empresa").textContent = err.message; }
};

$("#f-chave").onsubmit = async (ev) => {
  ev.preventDefault();
  const r = await api("POST", "/conta/chaves", { nome: ev.target.nome.value });
  const p = $("#chave-nova"); p.hidden = false; p.textContent = `Sua chave: ${r.chave} (${r.aviso})`;
  ev.target.reset(); carregar();
};

// UF e município vêm das tabelas da própria calculadora (motor da Receita).
fetch("/api/calculadora/dados-abertos/ufs").then((r) => r.json()).then((ufs) => {
  const sel = $("#f-empresa").uf;
  sel.innerHTML = '<option value="">Escolha</option>' + ufs.sort((a, b) => a.sigla.localeCompare(b.sigla)).map((u) => `<option>${esc(u.sigla)}</option>`).join("");
  sel.onchange = async () => {
    const m = $("#f-empresa").municipio;
    m.disabled = true;
    const lista = await (await fetch(`/api/calculadora/dados-abertos/ufs/municipios?siglaUf=${encodeURIComponent(sel.value)}`)).json();
    m.innerHTML = lista.map((x) => `<option value="${x.codigo}">${esc(x.nome)}</option>`).join(""); m.disabled = false;
  };
}).catch(() => {});

// ---------------------------------------------------------------- NF-e de produto (modelo 55)
// O servidor monta, assina e manda à SEFAZ da UF da empresa; aqui só se junta o pedido e se mostra o que voltou.
const ESTADOS_NFE = { ...ESTADOS, denegada: "Denegada" };
const fNfe = $("#f-nfe"), fCad = $("#f-nfe-cadastro"), selNfe = $("#nfe-empresa");

function nfeEmpresas(eu) {
  const atual = selNfe.value;
  selNfe.innerHTML = eu.empresas.map((e) => `<option value="${esc(e.id)}">${esc(e.razao)}</option>`).join("");
  if (atual) selNfe.value = atual;
  const fiscal = fNfe.querySelector('input[value="producao"]');
  fiscal.disabled = !eu.producao;
  if (!eu.producao) fNfe.querySelector('input[value="teste"]').checked = true;
  marcarAmbienteNfe();
  if (!$("#nfe-itens").children.length) linhaItem();
  if (selNfe.value) carregarNfe(selNfe.value);
}

async function carregarNfe(eid) {
  const cad = await api("GET", `/conta/empresas/${eid}/nfe/cadastro`);
  for (const k of ["ie", "fantasia", "telefone", "cep", "logradouro", "numero", "complemento", "bairro", "municipio", "serie_teste", "serie_producao"]) fCad[k].value = cad[k] || "";
  fCad.proximo_producao.placeholder = String(cad.proximo_producao);
  const regimeOk = ["simples", "mei"].includes(cad.regime);
  $("#nfe-cadastro-resumo").textContent = !regimeOk ? "Esta empresa é do regime normal: a NF-e por aqui ainda é só para Simples Nacional e MEI."
    : cad.pronto ? `Cadastro da NF-e: pronto (${cad.uf}, série de teste ${cad.serie_teste || 1}, próxima com valor fiscal nº ${cad.proximo_producao})`
    : "Cadastro da NF-e: falta preencher (inscrição estadual e endereço)";
  $("#nfe-cadastro-caixa").open = regimeOk && !cad.pronto;
  $("#b-nfe").disabled = !(regimeOk && cad.pronto);
  notasNfe(eid);
}

async function notasNfe(eid) {
  const { notas: lista } = await api("GET", `/conta/empresas/${eid}/nfe`);
  const base = `/conta/empresas/${esc(eid)}/nfe`;
  $("#nfe-notas").innerHTML = lista.length ? `<table><thead><tr><th>Quando</th><th>Destinatário</th><th class="num">Valor</th><th>Situação</th></tr></thead><tbody>
    ${lista.map((n) => `<tr><td>${esc(n.criada.slice(0, 16).replace("T", " "))}</td><td>${esc(n.destinatario)}</td><td class="num">${brl(n.valor)}</td>
      <td>${n.producao ? "" : '<span class="etiqueta">teste</span> '}${esc(ESTADOS_NFE[n.status] || n.status)} · série ${esc(n.serie)} nº ${esc(n.numero)}${n.status === "emitida" ? ` · <a href="${base}/${esc(n.id)}/pdf" target="_blank" rel="noopener">DANFE</a> · <a href="${base}/${esc(n.id)}/xml">XML</a>` : ""}${errosHtml(n)}</td></tr>`).join("")}
    </tbody></table>` : '<p class="dica">Nenhuma NF-e ainda.</p>';
}

function linhaItem(item = {}) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input name="i_codigo" maxlength="60" required placeholder="Código" aria-label="Código" value="${esc(item.codigo || "")}"></td>
    <td><input name="i_descricao" maxlength="120" required placeholder="Descrição" aria-label="Descrição" value="${esc(item.descricao || "")}"></td>
    <td><input name="i_ncm" inputmode="numeric" required placeholder="NCM (8 dígitos)" aria-label="NCM" value="${esc(item.ncm || "")}"></td>
    <td><input name="i_quantidade" inputmode="decimal" required placeholder="Qtd." aria-label="Quantidade" value="${esc(item.quantidade || "1")}"></td>
    <td><input name="i_valor" inputmode="decimal" required placeholder="Valor un. (R$)" aria-label="Valor unitário" value="${esc(item.valor_unitario || "")}"></td>
    <td><select name="i_csosn" aria-label="CSOSN"><option>102</option><option>103</option><option>300</option><option>400</option><option>500</option></select></td>
    <td><button type="button" class="secundario mini" aria-label="Tirar item">×</button></td>`;
  tr.querySelector("button").onclick = () => { if ($("#nfe-itens").children.length > 1) { tr.remove(); totalNfe(); } };
  tr.querySelectorAll("input").forEach((i) => i.addEventListener("input", totalNfe));
  $("#nfe-itens").appendChild(tr);
}

function itensDoForm() {
  return [...$("#nfe-itens").children].map((tr) => ({ codigo: tr.querySelector('[name="i_codigo"]').value.trim(),
    descricao: tr.querySelector('[name="i_descricao"]').value.trim(), ncm: tr.querySelector('[name="i_ncm"]').value.replace(/\D/g, ""),
    quantidade: tr.querySelector('[name="i_quantidade"]').value.trim(), valor_unitario: tr.querySelector('[name="i_valor"]').value.trim(),
    csosn: tr.querySelector('[name="i_csosn"]').value }));
}

function totalNfe() {
  const soma = itensDoForm().reduce((s, i) => s + num(i.quantidade) * num(i.valor_unitario), 0) + num(fNfe.frete.value);
  $("#nfe-total").textContent = soma > 0 ? `Total da nota: ${brl(soma)}` : "";
}

async function cepPara(f, saida) {
  const cep = f.cep.value.replace(/\D/g, "");
  if (cep.length !== 8) return;
  saida.textContent = "Buscando o CEP…";
  try {
    const d = await (await fetch(`https://viacep.com.br/ws/${cep}/json/`)).json();
    if (d.erro) { saida.textContent = "CEP não encontrado: preencha à mão."; return; }
    f.logradouro.value ||= d.logradouro || ""; f.bairro.value ||= d.bairro || "";
    f.municipio.value = d.localidade || f.municipio.value;
    if (f.municipio_ibge) { f.municipio_ibge.value = d.ibge || ""; f.uf.value = d.uf || ""; f.municipio.value = d.localidade ? `${d.localidade}` : ""; }
    saida.textContent = `Endereço preenchido pelo CEP (${d.localidade}/${d.uf}). Confira e coloque o número.`;
    f.numero.focus();
  } catch { saida.textContent = "Não consegui consultar o CEP agora."; }
}

function marcarAmbienteNfe() {
  const fiscal = fNfe.ambiente.value === "producao", b = $("#b-nfe");
  b.textContent = fiscal ? "Emitir NF-e com valor fiscal" : "Emitir NF-e de teste";
  b.classList.toggle("botao-fiscal", fiscal);
}

selNfe.onchange = () => carregarNfe(selNfe.value);
$("#nfe-mais-item").onclick = () => linhaItem();
fNfe.frete.addEventListener("input", totalNfe);
fNfe.cep.addEventListener("blur", () => cepPara(fNfe, $("#r-nfe-cep")));
fCad.cep.addEventListener("blur", () => cepPara(fCad, $("#r-nfe-cadastro")));
fNfe.querySelectorAll('input[name="ambiente"]').forEach((r) => { r.onchange = marcarAmbienteNfe; });

fCad.onsubmit = async (ev) => {
  ev.preventDefault();
  const d = Object.fromEntries(["ie", "fantasia", "telefone", "cep", "logradouro", "numero", "complemento", "bairro", "municipio", "serie_teste", "serie_producao", "proximo_producao"].map((k) => [k, fCad[k].value.trim()]));
  try { await api("PUT", `/conta/empresas/${selNfe.value}/nfe/cadastro`, d); $("#r-nfe-cadastro").textContent = "Cadastro salvo."; fCad.proximo_producao.value = ""; carregarNfe(selNfe.value); }
  catch (err) { $("#r-nfe-cadastro").textContent = err.message; }
};

fNfe.onsubmit = async (ev) => {
  ev.preventDefault();
  const f = fNfe, saida = $("#r-nfe"), ambiente = f.ambiente.value;
  if (!f.municipio_ibge.value) { saida.textContent = "Preencha o CEP do destinatário (ele traz o município)."; return; }
  if (ambiente === "producao" && !confirm("Emitir NF-e COM VALOR FISCAL? Ela vale para a SEFAZ.")) return;
  const destinatario = Object.fromEntries(["documento", "nome", "ie", "email", "cep", "logradouro", "numero", "complemento", "bairro", "municipio_ibge", "municipio", "uf"].map((k) => [k, f[k].value.trim()]));
  saida.textContent = "Enviando à SEFAZ…";
  try {
    const n = await api("POST", `/conta/empresas/${selNfe.value}/nfe`, { destinatario, itens: itensDoForm(), frete: f.frete.value,
      forma_pagamento: f.forma_pagamento.value, presencial: f.presencial.checked, informacoes: f.informacoes.value, ambiente, ref: `painel-${Date.now()}` });
    saida.textContent = n.status === "emitida" ? `NF-e autorizada: série ${n.serie} nº ${n.numero}.`
      : n.status === "desconhecida" ? "A SEFAZ não respondeu. Ela aparece na lista assim que confirmar."
      : `${ESTADOS_NFE[n.status] || n.status}: ${n.erros.map((x) => `${x.codigo} ${x.mensagem}`).join("; ")}`;
    notasNfe(selNfe.value);
  } catch (err) { saida.textContent = err.message; }
};

carregar();
