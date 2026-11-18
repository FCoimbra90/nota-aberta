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
  const outros = planos.filter((x) => x.checkout && x.notas_mes > p.notas_mes);
  $("#plano").innerHTML = `<h2>Plano ${esc(p.nome)}</h2>
    <p>${usadas} de ${p.notas_mes} notas usadas neste mês · até ${p.empresas} empresa(s)</p>
    <div class="acoes">${outros.map((o) => `<a class="botao" href="${esc(o.checkout)}">${esc(o.nome)}: R$ ${o.preco_mes}/mês, 30 dias grátis</a>`).join("")}
    ${outros.length ? '<button type="button" class="secundario" id="ja-assinei">Já assinei: atualizar</button>' : ""}</div>
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
    caixa.appendChild(el);
  }
  const sel = $("#f-emitir").empresa, atual = sel.value;
  sel.innerHTML = eu.empresas.map((e) => `<option value="${esc(e.id)}">${esc(e.razao)}</option>`).join("");
  if (atual) sel.value = atual;
  if (sel.value) notas(sel.value);
}

async function notas(eid) {
  const { notas: lista } = await api("GET", `/conta/empresas/${eid}/notas`);
  $("#notas").innerHTML = lista.length ? `<table><thead><tr><th>Quando</th><th>Tomador</th><th class="num">Valor</th><th>Situação</th></tr></thead><tbody>
    ${lista.map((n) => `<tr><td>${esc(n.criada.slice(0, 16).replace("T", " "))}</td><td>${esc(n.tomador || "—")}</td><td class="num">${brl(n.valor)}</td>
      <td>${esc(ESTADOS[n.status] || n.status)}${n.numero_nfse ? ` nº ${esc(n.numero_nfse)}` : ""}${n.status === "emitida" ? ` · <a href="/conta/empresas/${esc(eid)}/notas/${esc(n.id)}/pdf" target="_blank" rel="noopener">PDF</a>` : ""}${n.erros.length ? `<br><span class="erro-curto">${esc(n.erros.map((x) => `${x.codigo} ${x.mensagem}`).join("; "))}</span>` : ""}</td></tr>`).join("")}
    </tbody></table>` : '<p class="dica">Nenhuma nota ainda.</p>';
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
  const { planos } = await api("GET", "/conta/planos");
  $("#fora").hidden = true; $("#dentro").hidden = false;
  $("#quem").textContent = EU.usuario.email;
  const amb = $("#ambiente");
  amb.hidden = false;
  amb.textContent = EU.producao ? "Produção: as notas têm valor fiscal." : "Ambiente de teste do governo (produção restrita): as notas NÃO têm valor fiscal.";
  amb.classList.toggle("faixa-teste", !EU.producao);
  plano(EU, planos); empresas(EU); chaves(EU);
}

$("#f-emitir").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target, saida = $("#r-emitir");
  const tomador = f.doc.value.trim() ? { documento: f.doc.value, nome: f.nome.value, email: f.email.value } : undefined;
  saida.textContent = "Enviando ao governo…";
  try {
    const n = await api("POST", `/conta/empresas/${f.empresa.value}/notas`, { codigo_servico: f.codigo_servico.value, descricao: f.descricao.value,
      valor: f.valor.value, tomador, ref: `painel-${Date.now()}` });
    saida.textContent = n.status === "emitida" ? `Nota emitida${n.numero_nfse ? ` nº ${n.numero_nfse}` : ""}.`
      : n.status === "desconhecida" ? "O governo não respondeu. Ela aparece na lista assim que confirmar." : `Recusada: ${n.erros.map((x) => x.mensagem).join("; ")}`;
    carregar();
  } catch (err) { saida.textContent = err.message; }
};
$("#f-emitir").empresa.onchange = (ev) => notas(ev.target.value);

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

carregar();
