# nota-aberta: notas de operação

Calculadora grátis e pública de **CBS, IBS estadual e IBS municipal** (Reforma Tributária do Consumo,
LC 214/2025), em `reforma.genesisinnovation.io`. Pedido do Thiago em 17/11/2026: *"sobe um site pra calcular
a fórmula, por público"*, apontando para https://github.com/ReceitaFederal.

## No ar desde 18/11/2026

`https://reforma.genesisinnovation.io` no cluster `fabrica` (namespace `sistemas`, PVC próprio, segredos pelo
ExternalSecret `nota-aberta`), por ordem do Thiago (*"pode aplicar a exceção e pôr no ar"*). Conferido no ar:
páginas e `/api` 200, item 3 do exemplo oficial com `vCBS` 42.24, `/conta/eu` 401 sem login, login Google com o
`redirect_uri` aceito pelo cliente OAuth da casa. Emissão em **produção restrita** (sem valor fiscal) até a
primeira nota aceita pela Sefin com certificado real.

**Planos (20/11, ordem do Thiago)**: *"faz ilimitado mesmo; a gente vai cobrar só espaço de armazenamento"* e, para agora,
**tudo grátis**: notas e empresas sem limite, sem plano pago no site (`planos.json` com um plano só; `null` = sem limite). A
cobrança futura é de armazenamento, com uso medido antes (XML de uma NFS-e: ~9 KB; o PDF não é guardado). As tabelas anteriores
(29/99 com 30 dias; a do Rafa, 29,90 a 199,90; Contador/Escritório) saíram sem nenhum assinante (conferido no `/api/assinaturas`);
os links ficam no Pagar sem uso. Risco aceito: conta grátis sem teto pela API; se aparecer abuso, o freio é teto por minuto.

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

### Teste ou valor fiscal: escolha de cada nota (18/11/2026)

Pedido do Thiago: *"coloca as opções na tela de teste e valor fiscal"*. Cada pedido diz `ambiente`: `teste` (padrão,
produção restrita, sem valor fiscal) ou `producao` (nota de verdade). `NOTA_PRODUCAO=1` no pod passou a significar "o
servidor PERMITE nota fiscal"; sem ele, `producao` dá 403 antes de gastar cota. A nota guarda o ambiente em que saiu, e
consulta, reenvio e cancelamento usam esse, nunca o do servidor. O painel mostra o tipo antes do botão, o botão vira
"Emitir nota com valor fiscal" em vermelho e pede confirmação; a lista marca as notas de teste.

### Pedidos do 1º contador (Beto, 18/11/2026)

80% das notas dele são contrato de prefeitura: a mesma nota todo mês, muda a competência. O painel ganhou **Repetir** (preenche
o formulário com serviço, tomador, valor e tipo da nota escolhida, também das recusadas), **Competência** aberta (padrão hoje,
aceita mês anterior: serviço de outubro emitido em novembro, para o tomador do Lucro Real aproveitar o crédito) e **NBS**
opcional. A API já aceitava `competencia` e `nbs`; faltava a tela. A lista devolve `servico` em cada nota para o Repetir.

**Endereço do tomador (19/11)**: pedido do Beto para o optante do Simples. `Tomador` ganhou `cep`, `municipio_ibge`,
`logradouro`, `numero`, `complemento` e `bairro` (`<end><endNac>` na ordem do XSD 1.01, conferida no `tiposComplexos_v1.01.xsd`
do nfelib 3.3.0); vai inteiro ou não vai. O painel busca o CEP no ViaCEP (CSP liberada só para `viacep.com.br`), que devolve
o código IBGE. Provado na produção restrita: NFS-e nº 3 da Kiki Bags, cStat 100, com o endereço completo e `dCompet`
2026-10-31 (competência do mês anterior aceita pelo governo).

### Primeira NFS-e com valor fiscal (19/11/2026)

O Beto (contador, Ipu/CE) emitiu pelo painel a **NFS-e nº 1 em produção** às 16:41 (`tpAmb` 1, cStat 100, com NBS) e a nº 2,
que **cancelou pelo botão** às 17:01: o leiaute 1.01 de produção, o NBS e o evento de cancelamento em produção passaram de
primeira. Conferido no XML guardado de cada nota (não pelo status do painel).

### NF-e de produto de qualquer empresa (20/11/2026)

Ordem do Thiago ("pra várias, né"): a NF-e modelo 55 que só a Kiki emitia (`apps/factory`, uma empresa, só RJ) vira
capacidade da Nota Aberta, para toda empresa da conta, no mesmo login, no mesmo A1 cifrado, na API e no MCP. É o
módulo fiscal de produto que o James Negócio vai usar (plano em `docs/produtos/james-negocio.md`).

- **`nfe.py`**: porte do emissor da casa sem lxml nem erpbrasil. XML já canônico, assinatura de `dps.assinar` com
  RSA-SHA1, TLS mútuo de `sefin.contexto_tls` + raízes ICP-Brasil (`icp-brasil-raizes.pem`, cópia pública do da
  factory). Escopo: Simples Nacional e MEI, CSOSN 102/103/300/400/500, PIS/Cofins 49; regime normal é recusado antes da
  SEFAZ (ICMS próprio, ST e difal pedem outro desenho).
- **27 UFs**: `sefaz_nfe.json` (gerado da nfelib 3.3, MIT; não editar à mão). Medido em 20/11 com o A1 da Kiki: as 12
  autorizadoras que cobrem as 27 UFs (AM, BA, GO, MG, MS, MT, PE, PR, RS, SP, SVAN, SVRS) responderam `107 Serviço em
  Operação` na homologação. **Só a SVRS foi medida emitindo**; a 1ª nota de outra UF é a prova dela.
- **`emissor_nfe.py`**: cadastro fiscal por empresa (IE, endereço, série de teste e de produção; a UF sai do município da
  empresa), numeração por empresa/ambiente/série com "próximo número" que só sobe (para continuar a numeração do Bling
  ou de outro sistema), e as salvaguardas: `ref` idempotente; chave e XML assinado gravados antes do envio; sem resposta
  = desconhecida e a volta consulta a chave (217 reenvia o MESMO XML); 539 pula número (até 5); denegada gasta o número;
  recusada devolve número (se foi o último) e cota.
- **Provas na homologação SVRS (Kiki, série 97, destinatário em SC)**: nº 1 pelo `nfe.py` (cStat 100, protocolo
  333260001074257, consulta pela chave = 100); **nº 2 pelo emissor completo**, que começou do 1, levou 539 da SEFAZ e
  pulou sozinho (protocolo 333260001074259); repetir o mesmo `ref` devolveu a mesma nota sem reenviar. XML do nfeProc e
  DANFE (BrazilFiscalReport 1.4.0) gerados das duas.
- **API**: `GET|PUT /conta/empresas/<id>/nfe/cadastro`, `GET|POST /conta/empresas/<id>/nfe`,
  `GET /conta/empresas/<id>/nfe/<nota>/xml|pdf` (download com o nome da chave). **MCP**: `cadastro_nfe`, `emitir_nfe`,
  `listar_nfe`. **Painel**: seção "Nota de produto (NF-e)" com cadastro, CEP pelo ViaCEP, itens, frete, pagamento e
  Teste/Valor fiscal.
- **Fora desta versão**: regime normal, cancelamento e carta de correção da NF-e (eventos 110111/110110), inutilização de
  número (lacuna deixada por recusada no meio), NFC-e e devolução. Dívida declarada: o emissor da `apps/factory` (Kiki,
  Bling) segue sendo uma segunda cópia; o caminho é a Kiki virar uma empresa daqui e ele ser aposentado.

### IBS/CBS da Reforma e retenções na nota (20/11/2026)

Ordem do Thiago: a nota tem de servir ao Lucro Real e sair já com a Reforma. O que mudou e o que foi medido:

- **IBSCBS na DPS.** Com NBS, `rtc_nfse.classificar` pergunta ao motor oficial do próprio pod (rotas `calculadora/nfse/*`
  do jar 1.6.1): NBS 115021000 -> CST 000, cClassTrib 000001, cIndOp 100301. Mais de uma opção = 422 com as opções
  (ninguém escolhe pela pessoa); `ibscbs: false` emite sem o grupo; sem NBS a nota sai como antes. Quem calcula CBS e
  IBS é a Sefin, na nota; a DPS só classifica.
- **Dois esquemas, medidos na Sefin.** Produção restrita valida o 1.02 (grupo começa em `indFinal`; `CST`/`cClassTrib`
  direto em `trib`). **Produção ainda valida o esquema anterior**: o grupo começa em `finNFSe` e `CST`/`cClassTrib` vão
  dentro de `gIBSCBS`. Medido sem emitir: DPS para a produção com `tpAmb` 2 e `cTribNac` 999999 (não pode virar nota);
  com o formato errado volta E1235 (esquema), com o certo volta E0006 (ambiente), ou seja, passou do esquema. Quando a
  produção trocar de esquema, o teste `test_ibscbs_na_producao_segue_o_esquema_antigo...` é o que muda junto.
- **Retenções.** `retencoes` em reais: ISS retido (`tpRetISSQN` 2) com `pAliq`, IRRF, CSLL, INSS (`vRetCP`), PIS e Cofins
  (`piscofins`, CST 01, `tpRetPisCofins` pelo que foi retido). ME/EPP com ISS retido sem alíquota é recusado aqui, como a
  Sefin faria (E0621, mínimo 1,8%).
- **Alíquota do Simples (21/11/2026, pedido de um usuário no X).** `aliquota_simples` (%, ex.: 6,00) vai em
  `totTrib/pTotTribSN`, como o campo do emissor oficial; vazio continua saindo `0`, como antes. Só ME/EPP do Simples:
  MEI e regime normal levam 422 antes do governo (E0710 a E0713). Faixa aceita de 0 a 33% (maior alíquota nominal do
  Simples). O painel mostra o campo só para empresa do Simples e lembra a última no navegador; Repetir copia.
- **Conferir antes de emitir (21/11/2026, pedido no X).** `conferir_nfse` (MCP) e `POST /conta/empresas/<id>/notas/conferir`
  devolvem `pronto`, `faltando` (bloqueia: certificado, campos obrigatórios, endereço pela metade, alíquota do ISS retido,
  e o resto pela mesma `_preparar` + `dps.montar` do emitir, sem enviar) e `perguntar` (muda a nota: tomador, alíquota do
  Simples, ambiente). Não gasta cota, não abre o certificado, não fala com o governo. O `emitir_nfse` manda chamar antes.
- **Provas na produção restrita (Kiki Bags, Florianópolis):** nº 4 com IBSCBS (base 100,00, CBS 0,90, IBS 0,10); nº 5 com
  retenções e IBSCBS; **nº 6 pelo código desta versão, sem remendo no XML**: R$ 1.000,00, ISS retido 2%, IRRF 15, CSLL 10,
  INSS 110, PIS 6,50, Cofins 30. A Sefin devolveu base IBS/CBS **943,50, CBS 8,49 e IBS 0,94, iguais à prévia do painel**
  (base de cálculo e alíquotas de referência do mesmo motor). Na nota, `vTotalRet` 135,00 e `vLiq` 865,00: a Sefin não
  desconta o ISS retido nem o PIS/Cofins no líquido (medido; o painel mostra o que ela devolve, não uma conta nossa).
- **Painel:** caixa "Reforma Tributária" com a classificação, a prévia e "Emitir sem o grupo"; "Retenções na fonte"; na
  lista, CBS, IBS, ISS, retido e líquido lidos da NFS-e (`tributos_da_nota`). Repetir copia as retenções.
- **API pública:** `GET /api/calculadora/nfse/{situacoes-classificacoes-tributarias,indicador-operacao,local-operacao,
  locais-fornecimento,locais-incidencia}` (dados abertos do motor). MCP: `classificar_nbs`; `emitir_nfse` com `ibscbs` e
  `retencoes`.
- **Defeito achado no caminho:** valor "1.000,00" era recusado (`float("1.000.00")`); `numero_br` aceita os dois jeitos.

### Inscrição municipal e regime especial de tributação (21/11/2026)

Duas lacunas que barravam contador e prefeitura: a IM do prestador nunca era preenchida e o `regEspTrib` saía fixo `0`.

- **Dado da empresa**, colunas `inscricao_municipal` (texto, padrão vazio) e `regime_especial` (padrão `0`) em
  `empresas`. Banco que já existe ganha as duas por `alter table` na abertura, só se faltarem (`contas.COLUNAS_NOVAS`;
  teste com banco no formato antigo que migra e continua emitindo).
- **Editar**: `PATCH /conta/empresas/<id>` (só esses dois campos; o resto do corpo é ignorado), o formulário
  "Dados fiscais" no cartão da empresa e a ferramenta MCP `editar_empresa`. Também aceitos no cadastro da empresa.
- **Leiaute conferido no XSD 1.01** (nfelib 3.3.0, `nfse/schemas/v1_0/tiposSimples_v1.01.xsd`): `TSInscMun` é texto de
  1 a 15 caracteres e vai em `prest` depois do CNPJ (opcional); `TSRegEspTrib` aceita 0 nenhum, 1 ato cooperado, 2
  estimativa, 3 microempresa municipal, 4 notário ou registrador, 5 profissional autônomo, 6 sociedade de profissionais
  e **9 outros** (o 9 não estava na lista de partida e foi incluído por ser do XSD). Aqui a IM tira `.`, `-`, `/` e
  espaço e aceita só letra e dígito (mais estreito que o XSD, para não mandar máscara). DPS com IM e regime 6 e 9
  validou no `DPS_v1.01.xsd` da nfelib (esquema da produção restrita). Nenhuma regra de compatibilidade entre regime
  especial e Simples foi inventada: quem recusar combinação é a Sefin, e a recusa volta com o código dela.

### Primeira NFS-e aceita (18/11/2026, produção restrita)

Kiki Bags (Rio/RJ, A1 do cofre `mybagcenter/NFE_A1_67520388000122_*`), código 11.02.01 com local da prestação em
Florianópolis: **NFS-e nº 1, cStat 100**, chave `33045572267520388000122000000000000126119389453189`, conferida depois por
`GET /nfse/<chave>` (200, XML) e com o PDF do DANFSe gerado no pod. Nota nº 2 (16.01.01) também aceita. Feito por script
fora do banco do site (`~/state/maestro/na-teste/` na factory-01, banco de teste próprio).

Medido no mesmo dia, com `GET https://adn[.producaorestrita].nfse.gov.br/parametrizacao/<IBGE>/convenio` (mTLS):

| município | teste (produção restrita) | produção |
|---|---|---|
| Ipu/CE 2305803 (1º usuário de fora) | **inativo** → E0037 | ativo |
| Rio de Janeiro 3304557 | ativo, mas **sem alíquota cadastrada** (E0312 em 18 códigos comuns) | ativo |
| Florianópolis 4205407 | ativo, alíquotas cadastradas (2%) | ativo |

**Cancelamento aceito (18/11/2026, produção restrita)**: evento 101101 da nota nº 2 da Kiki Bags, `POST /nfse/<chave>/eventos`
devolveu 201 com o `eventoXml`, e `GET /nfse/<chave>/eventos/101101/1` devolve 200 depois. O emissor cancela no ambiente em
que a nota saiu (`notas.producao`), não no do servidor, e consulta o evento antes de dizer que falhou.

Alíquota por serviço: `GET .../parametrizacao/<IBGE>/<cc.ss.dd.ddd>/<AAAA-MM-DDT00:00:00>/aliquota` (o código vai **com
pontos**, ex. `01.01.01.000`; sem pontos dá 400 "nove dígitos"). Itens 07.02/07.04/... pedem o grupo de obra (E0370) e o
item 12 o de evento (E0390): ainda não montamos esses grupos. Consequência: o teste do governo não serve de prova para
todo município; quem está em município inativo no teste só emite em produção (`NOTA_PRODUCAO=1`, decisão do dono).

### Segredos que o pod pede (nunca no Git; `apps/factory/bin/pacote_segredo.py nota-aberta ...`)

| variável | o que é | sem ela |
|---|---|---|
| `NOTA_CHAVE_MESTRA` | 32 bytes em base64, embrulha a chave de cada empresa | certificado não é guardado nem aberto (503) |
| `NOTA_SEGREDO_SESSAO` | 32+ bytes, assina o cookie de sessão | a conta inteira fica desligada (503), a calculadora segue |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | o cliente OAuth da Genesis (cofre `genesis-ferramentas`); redirect `https://<host>/entrar/google/retorno` por `google-oauth-uri.py --gravar` | o botão "Entrar com Google" responde 503 |
| `NOTA_PAINEL_EMAILS` (opcional) | e-mails (vírgula) que entram no painel de uso `/operacao` pelo login Google | o painel responde 403 para todos |
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
- **Ouvinte da página antes de qualquer `await`** (18/11, no ar): quem escolhia a UF enquanto as tabelas ainda
  carregavam ficava sem municípios, porque o `onchange` só era ligado depois do `fetch`. Teste:
  `test_calculadora_liga_os_ouvintes_antes_de_esperar_as_tabelas` (#3622).
- **CD da imagem parado na fila com a `factory-ci` ligada e sem runner registrado** (18/11, 18 min): cura da casa,
  `receita_vm stop factory-ci` (sem job em andamento) e o `runner-farm` religa. Se o CD falhar em outra imagem
  (ex.: timeout de pip do genesis-musica), dispare `cd-imagens-k3s.yml` com `imagem=nota-aberta`.
- **Publicar não derruba mais o site** (corrigido em 18/11, #3644, depois de medir ~2 min de 502 por publicação): o
  Deployment é `RollingUpdate` (`maxSurge: 1`, `maxUnavailable: 0`). O PV é local-path no `fabrica-server-a`, sem anexo
  exclusivo, então o pod novo sobe no mesmo nó, só recebe tráfego depois do `/health` (motor Java de pé, ~1 min) e o
  antigo sai depois. Medido na publicação do #3645: 380 sondas de 1 s em `/health`, **380 com 200**. Os outros 6
  sistemas com volume seguem em `Recreate` até cada um ser conferido (tarefa em segundo plano rodaria em dobro).
- **O motor avisa "VERSÃO DESATUALIZADA"** no log (18/11: online 1.7.0, base V0073 de 09/11; local 1.6.1, V0059).
  Não suba por esse aviso: a versão vale quando o commit estiver registrado em Portaria (seção "Atualizar o motor").

## Fios abertos

- O MCP entra em `apps/genesis-site/mcp/servicos.json` (hub dos MCP públicos) depois de estar no ar: o `servicos.py medir` chama a URL de verdade.

- Simples Nacional e DANFSe ainda não estão aqui (próximo PR). Medido em 17/11: o jar do Simples (0.0.6)
  compila e sobe com o **mesmo** `calculadora-pro.db` do Regime Regular (o repo dele não traz `flyway/`; o README
  manda copiar a pasta) e responde `/api/versao` com a base V0059. A entrada dele pede a alíquota efetiva do
  Simples (`pIBSSN`, `pCBSSN`) e a atividade (`cAtivSN`), então a página precisa de um formulário próprio.
  O DANFSe é Go (gera PDF a partir do XML da NFS-e).
- Imposto Seletivo, NFS-e, base unificada e XML existem pela API; a página cobre o caso CBS/IBS por item.

## Painel de uso `/operacao` (21/11/2026)

No modelo do painel do Fichário: pedidos por minuto (60 min, 24 h, 7 dias), canais, quem usa (pessoa, agente de IA,
robô), origem das visitas (t.co = X), páginas, países, ferramentas MCP e as contagens de contas, empresas e notas.
Código em `uso.py`; grava em `uso.db` ao lado do `NOTA_DB` (`NOTA_USO_DB` troca), despejo a cada 15 s, 8 dias por
minuto e 31 nos rankings. Sem dado pessoal: IP só vira HMAC com chave do dia (visitante único), ranking corta o que
apareceu 1 vez. Entra quem fez login Google e está em `NOTA_PAINEL_EMAILS` (segredo do pod, nunca no Git); o botão da
página volta ao painel depois do login (`/entrar/google?volta=/operacao`, só destinos da lista `VOLTAS`).
Nasceu do lançamento no X (20/11): 30 mil pedidos em 4 h e "o link não abre" sem número para conferir.

### Funil, lista de empresas e erros explicados (21/11/2026)

- **/operacao** ganhou o **funil** (contas → com empresa → com certificado → emitiram alguma nota → com valor fiscal,
  contas únicas) e a **lista por empresa**: razão social, UF/IBGE, regime, etapa, notas de teste e com valor fiscal,
  recusadas e o último erro já explicado. Sem e-mail e sem CNPJ (`uso.SQL_EMPRESAS`). Medido no dia: 95 contas, 15
  empresas, 3 com certificado, 2 com nota de valor fiscal: o gargalo é subir o certificado.
- **Erro explicado** (`emissor.explicar`, aplicado na leitura em `publica`, então vale também para nota antiga):
  mensagem limpa (sem a página HTML que o 403 do governo devolve) + **o que fazer** (`dica`) + o código pequeno para o
  suporte. 401/403 no teste = instabilidade do governo; em produção = conferir o certificado; 5xx = governo fora;
  `entrada` = nada enviado, cota não gasta; E0234 = preencher o endereço do tomador. O painel mostra um quadro por erro;
  API e MCP devolvem os mesmos campos.

### Documentação da API e do MCP (21/11/2026)

Pedido no X: "tentei acessar o MCP e não deu. E onde está a documentação da API?". O `/mcp` só aceita POST (JSON-RPC)
e quem abria no navegador via `{"erro": "não encontrado"}`; não havia página de API. Agora:
- `/docs` (HTML) e `/llms.txt` (texto para agentes) são **gerados** por `docs.py` a partir de `mcp.FERRAMENTAS` +
  `mcp.CONTA_FERRAMENTAS` e da lista `docs.ROTAS`; `tests/test_docs.py` falha se uma ferramenta faltar na página ou se
  uma rota documentada não existir no `conta_web` (provado com uma rota inventada).
- `GET /mcp`: navegador vai para `/docs#mcp`; cliente MCP que pede SSE (`Accept: text/event-stream`) recebe 405 com
  `Allow: POST`, como manda a spec do Streamable HTTP; outro cliente recebe um JSON dizendo como usar.
- `/api`, `/api/` e `/api/docs` redirecionam para `/docs`. O Swagger do motor oficial segue em `/api/swagger-ui.html`.

## Indexação no Google e no Bing (21/11/2026)

`web/robots.txt` (fecha `/conta/`, `/entrar/`, `/operacao` e `/painel.html`), `web/sitemap.xml` (só páginas com
`index, follow` e canonical nelas mesmas, conferido por `tests/test_indexacao.py`), `favicon.ico`, JSON-LD
`SoftwareApplication` e `og:image` absoluta (o X só mostra imagem com URL completa). Google: propriedade
`sc-domain:genesisinnovation.io` pelas `search_console_*` do Factory MCP, sitemap enviado. Bing e Yandex: IndexNow,
chave pública em `web/nota-aberta-indexnow-*.txt` (o arquivo traz a própria chave). Página nova pública = linha no
sitemap + ping do IndexNow com a URL.
