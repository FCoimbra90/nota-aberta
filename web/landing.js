// Página oficial: os planos vêm de /conta/planos (planos.json no servidor), nunca escritos aqui.
"use strict";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

document.querySelectorAll(".host").forEach((el) => { el.textContent = location.host; });

fetch("/conta/planos").then((r) => r.ok ? r.json() : Promise.reject(r.status)).then(({ planos }) => {
  document.getElementById("planos").innerHTML = planos.map((p, i) => `
    <article class="plano${i === 1 ? " destaque-plano" : ""}">
      <h3>${esc(p.nome)}</h3>
      <p class="preco">${p.preco_mes ? `R$ ${esc(p.preco_mes)}<span>/mês</span>` : "R$ 0"}</p>
      <ul>${p.destaques.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
      <a class="botao${i === 1 ? "" : " secundario"}" href="/entrar/google">${p.preco_mes ? "Testar 30 dias" : "Começar grátis"}</a>
    </article>`).join("");
}).catch(() => {
  document.getElementById("planos").innerHTML = '<p class="dica">Os planos não carregaram agora. Tente de novo em instantes.</p>';
});
