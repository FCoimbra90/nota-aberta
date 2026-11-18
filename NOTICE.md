# Avisos de terceiros

O código deste repositório é MIT (`LICENSE`, Genesis Innovation). Ele **não inclui** o código dos componentes
abaixo: o `Dockerfile` baixa e compila cada um no commit fixado, e cada um segue a própria licença.

| componente | de quem | licença | como entra |
|---|---|---|---|
| Calculadora de Tributos - Regime Regular 1.6.1 (`github.com/ReceitaFederal/calculadora-rtc-regime-regular`, commit `150cccbe414d4a5edea703e4f7a424af5aa222c7`) | Receita Federal do Brasil | MIT | compilado sem alteração no build; a licença dela vai na imagem em `/rtc/LICENSE-receita-federal` |

A publicação do código da Receita foi autorizada pela Portaria Cotec nº 342, de 12 de novembro de 2026 (DOU de
13/11/2026), que registra os hashes das versões iniciais. Segundo a própria Receita, decisão judicial ou
interpretação tributária diferente pode exigir adaptações, e a publicação não implica validação de usos externos,
garantia de suporte, compromisso de manutenção ou efeito vinculante do código.

Componentes que vão entrar com a emissão de NFS-e: `nfelib` (MIT), `erpbrasil.assinatura` (MIT),
`BrazilFiscalReport` (LGPL-3.0, usada sem modificação) e o DANFSe Service da Receita (MIT).
