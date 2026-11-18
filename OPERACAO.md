# nota-aberta: notas de operação

Calculadora grátis e pública de **CBS, IBS estadual e IBS municipal** (Reforma Tributária do Consumo,
LC 214/2025), em `reforma.genesisinnovation.io`. Pedido do Thiago em 17/11/2026: *"sobe um site pra calcular
a fórmula, por público"*, apontando para https://github.com/ReceitaFederal.

## O que roda

| peça | de quem | onde |
|---|---|---|
| motor de cálculo + tabelas (CST, cClassTrib, NCM/NBS, alíquotas de referência) | **Receita Federal**, MIT, commit fixo em `RTC_COMMIT` do `Dockerfile` | `github.com/ReceitaFederal/calculadora-rtc-regime-regular` |
| porta (página, `/health`, repasse de `/api/*` com lista de rotas, teto por IP e limite de corpo) | nossa | `servidor.py` |
| página | nossa | `web/` |
| MCP (`POST /mcp`: `calcular_cbs_ibs`, `tabelas_cbs_ibs`, `aliquotas_referencia`, `versao_motor`; só leitura, mesmo teto por IP contado por chamada) | nosso, chama o mesmo motor | `mcp.py` |

**Nenhum valor de tributo é calculado por código nosso.** O Dockerfile compila o jar oficial sem tocar no
código deles; reimplementar a fórmula seria uma segunda cópia da regra fiscal, que diverge da oficial na
primeira atualização. `tests/test_servidor.py::test_pagina_nao_calcula_tributo_no_navegador` cobra isso.

## Emissão de NFS-e (18/11/2026)

| peça | arquivo | estado |
|---|---|---|
| contas, empresas, cota, chaves de API | `contas.py` | pronto, testado |
| cofre do certificado (envelope AES-256-GCM, AAD com o id da empresa) | `cofre.py` | pronto, testado |
| leitura do A1 (senha, validade, CNPJ do e-CNPJ) | `certificado.py` | pronto, testado com certificados gerados |
| DPS + cancelamento + assinatura RSA-SHA256 | `dps.py` | porte do emissor-nfse-nacional; assinatura conferida pelo C14N do lxml |
| cliente da Sefin Nacional com o A1 em memória (memfd) | `sefin.py` | pronto; TLS mútuo provado contra servidor local |
| emissor com salvaguardas (ref, desconhecida, E0014) | `emissor.py` | pronto, testado com Sefin falsa |
| login Google, rotas da conta, planos pelo Genesis Pagar | `oauth.py`, `conta_web.py`, `planos.py`, `planos.json` | pronto, testado |
| MCP de emissão | `mcp.py` | pronto, testado |
| página oficial, painel, PDF pelo DANFSe | `web/`, `conta_web.danfse` | pronto; fluxo inteiro no Chromium |

**Ainda não provado: uma nota aceita pela Sefin de verdade.** Tudo acima roda contra um governo falso. O primeiro teste
real é em produção restrita (`NOTA_PRODUCAO` ausente), com o A1 de uma empresa nossa, antes de qualquer cliente.
Pontos a conferir nesse teste: o endereço `sefin.producaorestrita.nfse.gov.br/SefinNacional/nfse` (vem do
emissor-nfse-nacional e da página do gov.br), o leiaute 1.02 da produção restrita e o formato da resposta.

### Segredos que o pod pede (nunca no Git; `apps/factory/bin/pacote_segredo.py nota-aberta ...`)

| variável | o que é | sem ela |
|---|---|---|
| `NOTA_CHAVE_MESTRA` | 32 bytes em base64, embrulha a chave de cada empresa | certificado não é guardado nem aberto (503) |
| `NOTA_SEGREDO_SESSAO` | 32+ bytes, assina o cookie de sessão | a conta inteira fica desligada (503), a calculadora segue |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | o cliente OAuth da Genesis (cofre `genesis-ferramentas`); redirect `https://<host>/entrar/google/retorno` por `google-oauth-uri.py --gravar` | o botão "Entrar com Google" responde 503 |
| `PAGAR_TOKEN` (opcional) | token da API do Genesis Pagar | todo mundo fica no plano grátis |

**Perder a `NOTA_CHAVE_MESTRA` é perder todos os certificados guardados** (os clientes sobem de novo). Ela vai para o
Secret Manager com versão, e trocá-la exige reembrulhar as chaves de dados (ainda não há script: fio aberto).

### Para ligar no cluster (decisão do dono, ainda não feito)

1. Cota do namespace `sistemas`: estava em 6/6 PVC e 7/7 Gi em 17/11. O volume de 1 Gi precisa de cota maior num PR
   promovido ANTES do app (o Flux faz dry-run de tudo e trava; regra de 05/11).
2. Segredos acima pelo `pacote_segredo.py`; links mensais dos planos no Genesis Pagar (`teste_dias: 30`) e o id em
   `planos.json`.
3. `sistema_novo.py publicar nota-aberta` e `rota` para `reforma.genesisinnovation.io`.
4. Primeira nota real em produção restrita; só então `NOTA_PRODUCAO=1`.

## Repositório público (espelho)

`github.com/thiagopatzdorf/nota-aberta`, público e MIT desde 18/11/2026 (decisão do Thiago: "é MIT, a ideia é se
posicionar como player"). A fonte é esta pasta; o público é espelho por `apps/factory/bin/publicar-repo.py <pasta>
nota-aberta --confirmar` na factory-01 (monte a pasta com `git archive <commit>:apps/sistemas/nota-aberta`). O script
recusa publicar por cima de PR de fora ainda não trazido para cá. Inversa: `--despublicar --confirmar` (volta a privado).

## Autorização e versão conferida

A Portaria Cotec nº 342, de 12/11/2026 (DOU de 13/11/2026, Seção 1 Extra B, p. 10), autoriza a publicação
dos três projetos sob MIT e registra os hashes das versões iniciais. Conferido em 17/11/2026 contra os clones:

| projeto | commit na Portaria | HEAD do repositório |
|---|---|---|
| Regime Regular 1.6.1 (este app) | `150cccbe414d4a5edea703e4f7a424af5aa222c7` | igual |
| Simples Nacional 0.0.6 | `c5b0f01c8351c3f0d0795cc617b29d233e9f98df` | igual |
| DANFSe Service 1.0.0 | `8d79e6b2f7fad6375340afcb51c85ed370c64a14` | igual |

A Portaria e a página avisam: decisão judicial ou interpretação tributária diferente pode exigir adaptação, e
a publicação não traz garantia, suporte nem efeito vinculante. A página repete isso ao público.

## Comandos

    # testes da porta (motor falso)
    python3 -m pytest -q tests/
    # paridade com o motor oficial (o item 3 de docs/exemplos.md da Receita: vCBS 42,24)
    RTC_MOTOR_REAL=http://127.0.0.1:8081 python3 -m pytest -q tests/
    # imagem completa
    docker build -t nota-aberta apps/sistemas/nota-aberta
    docker run --rm -p 8080:8080 --read-only --tmpfs /tmp nota-aberta

Para rodar o motor fora do Docker: clonar o repo da Receita, `mkdir -p calculadora/db`, `./mvnw flyway:migrate
-Dflyway.configFiles=./flyway/flyway-pro.conf` e `./mvnw -DskipTests package` (a pasta `calculadora/db` não vem
no repo e o flyway falha com `SQLITE_CANTOPEN` sem ela).

## Atualizar o motor

A Receita publica versão nova (tabelas novas, regra nova): troque `RTC_COMMIT` no `Dockerfile`, rode a
paridade contra o jar novo e abra o PR. O CD publica a imagem e troca o digest no cluster.

## Medidas (17/11/2026, motor 1.6.1, tabelas V0059)

- Memória do jar com as flags do `RTC_JAVA_OPTS`: ~365 MB de RSS; limite do pod 640 Mi (pedido 112 Mi).
- Partida do jar: ~10 s. Cálculo de 4 itens: ~30 ms depois do aquecimento.
- Alíquotas de referência só existem para 2026 (CBS 0,9%, IBS UF 0,1%, município 0%); para 2027 a API
  responde 404 e a página pede a alíquota nominal.

## Regras que nasceram de erro

- **Antes de 2027 o motor recusa alíquota informada** (`REG-036`): ele resolve as vigentes sozinho. A página
  só manda `aliquotasNominais` com fato gerador a partir de 01/01/2027.
- O Swagger do jar lê a configuração em `/api/api-docs/swagger-config` (springdoc com `api-docs.path`
  próprio), não em `/v3/api-docs`: a lista `PERMITIDAS` segue o caminho medido.

## Fios abertos

- O MCP entra em `apps/genesis-site/mcp/servicos.json` (hub dos MCP públicos) depois de estar no ar: o `servicos.py medir` chama a URL de verdade.

- Simples Nacional e DANFSe ainda não estão aqui (próximo PR). Medido em 17/11: o jar do Simples (0.0.6)
  compila e sobe com o **mesmo** `calculadora-pro.db` do Regime Regular (o repo dele não traz `flyway/`; o README
  manda copiar a pasta) e responde `/api/versao` com a base V0059. A entrada dele pede a alíquota efetiva do
  Simples (`pIBSSN`, `pCBSSN`) e a atividade (`cAtivSN`), então a página precisa de um formulário próprio.
  O DANFSe é Go (gera PDF a partir do XML da NFS-e).
- Imposto Seletivo, NFS-e, base unificada e XML existem pela API; a página cobre o caso CBS/IBS por item.
