# ADR-0002: correlacao por `trace_id` propagado (W3C `traceparent`), em vez de busca por texto

Status: aceito
Data: 2026-05-20
Decisao aceita em: 2026-05-27
Decisor: autor (SRE), com anuencia do time que escreve as aplicacoes
Contexto relacionado: `otel/tagging.md`, `app/server.py`,
`grafana/dashboards/correlacao-incidente.json`, `otel/collector-config.yaml`

## Contexto

Existe uma diferenca de minutos entre "sei qual request" e "sei o que aconteceu
com aquele request". Em ambiente com cinco servicos, o metodo que a maioria
adota por default e o mais caro: abrir o Loki, procurar uma frase do erro,
copiar um timestamp, refazer a busca em outro servico com outro padrao de
mensagem, e repetir.

Esse metodo tem quatro falhas estruturais:

1. **Mensagem de log muda.** Refatorar uma string quebra a busca que o runbook
   documenta. A busca por texto e uma dependencia oculta no codigo de outra
   pessoa.
2. **Timestamp nao e identificador.** Dois requests do mesmo usuario no mesmo
   segundo nao se separam; sem correlacao, o agregado continua ambiguo.
3. **Custo de query cresce com a desesperacao.** `|~ ".*timeout.*fail.*"` numa
   janela de 6h em um stream grande e exatamente o tipo de consulta que derruba
   um querier de Loki e nao responde nada.
4. **Nao atravessa o processamento assincrono.** Job de fila, webhook,
   reprocessamento noturno: a frase do log nao sobrevive ao salto de processo.

As alternativas concretas que avaliamos:

| Alternativa | Como funcionaria | Por que nao |
| --- | --- | --- |
| Busca por texto (status quo comum) | regex no Loki por servico | itens 1 a 4 acima |
| `request_id` proprio em header custom | app gera, loga e propaga | resolve o join, mas nao da arvore causal; vira segunda classe quando existe W3C |
| Correlacao por timestamp + usuario | join implicito | ambiguo, caro, e nao funciona em assincrono |
| **`trace_id` W3C propagado, escrito em todo log e span** | um identificador de 128 bits por request | e a escolha deste ADR |
| APM de mercado com auto-instrumentacao | agente proprietary | viola requisito 1 do projeto |

A norma W3C Trace Context define `traceparent` como
`version-trace_id-parent_id-flags` (por exemplo
`00-<32 hex>-<16 hex>-01`). Jaeger e OpenTelemetry adotam a mesma sintaxe, o
que torna a decisao portavel entre backends.

## Decisao

Toda request que entra no sistema ganha contexto de traco na borda, e esse
contexto e:

1. **propagado** no cabecalho `traceparent` em cada salto, com `X-Request-Id`
   transportado junto como identificador do ponto de vista do cliente;
2. **escrito em todo log** no campo `trace_id` (hex de 32), com `span_id` e
   `parent_span_id` quando houver;
3. **usado como chave de join** entre Loki e Tempo, via `derivedFields` no
   painel de logs, apontando para o datasource Tempo;
4. **registrado na resposta HTTP** em `X-Trace-Id`, para que o chamado externo
   (cliente, suporte, outro time) consiga citar o mesmo request que nos vemos;
5. **validado** em runtime: o Collector rejeita e conta log de request sem
   contexto quando o pipeline espera contexto, e a app marca
   `propagated=false` quando chegou sem `traceparent` valido.

Implementacao minima neste repositorio, para que a decisao seja verificavel sem
dependencia de SDK: `app/server.py` faz parse/geracao de `traceparent` em
stdlib, `app/loadgen.py` encadeia dois passos de uma jornada com o mesmo
`trace_id` e `otel/collector-config.yaml` normaliza o atributo nos pipelines.

Nao decidimos indexar `trace_id` como label de metrica: 128 bits de cardinalidade
por request e a maneira mais rapida de derrubar um TSDB. `trace_id` vive em log e
span. Quando precisamos amarrar metrica ao trace, usamos **exemplar**, que
carrega o id no ponto da serie, nao como label.

## Consequencias

**Positivas**

- O painel `Correlacao de incidente` existe: log com erro vira arvore de trace
  em um clique, e a arvore vira os logs daquele trace na mesma tela.
- `propagated=false` virou um indicador operacional. E a melhor diagnostic
  deste stack: mostra exatamente onde o contexto se perde, servico a servico.
- Buscar "todos os logs deste request" funciona entre servicos sem conhecer o
  vocabulario de log de ninguem.
- A pergunta "foi erro nosso ou da dependencia?" passa a ter resposta visual
  (span SERVER vs span CLIENT com `error.type`).

**Negativas**

- **Custo de instrumentacao em codigo legado.** Todo servico que nao propaga
  `traceparent` cria um corte na historia: os traces param no front dele. Isso e
  trabalho de time de produto, e aparece como backlog, nao como bug.
- **Amostragem passa a ser decisao com consequencia visivel.** Se amostramos 10%
  dos traces, 90% dos logs vao apontar para um trace que nao existe no Tempo. A
  resposta e amostragem baseada em erro (`tail sampling` no Collector) em vez de
  head sampling uniforme, e o `file/traces-debug` no config guarda erro por mais
  tempo que o caminho feliz.
- **Um identificador novo para explicar em onboarding.** `trace_id` (causal) e
  `X-Request-Id` (recibo do cliente) parecem redundantes para quem chega. Nao
  sao, e o `otel/tagging.md` traz a tabela de quando cada um e a chave.
- **Vazamento potencial de identificadores em URL de suporte.** Se um link de
  trace for publicado em ticket publico, expoe estrutura interna. Mitigacao:
  os links do painel sao internos ao Grafana, sem token embutido.

## Como medir que acertou

1. **Percentual de propagacao por servico.** `propagated=false` em queda no Loki
   e a medida primaria. Alvo inicial: > 98% dos requests com contexto valido em
   cada salto do caminho de checkout (sintetico aqui; em ambiente real, medir
   antes de prometer).
   ```bash
   docker compose exec loki logcli --address http://localhost:3100 query \
     '{service="checkout-api"} | json | propagated="false"' --limit 5
   ```
2. **Tempo entre "tenho um trace_id" e "sei a causa"**, medido em simulacro com
   o `rb-taxa-erro.md` executado de verdade, com cronometro e duas pessoas.
3. **Numero de buscas por texto em janela > 1h por semana** no Loki. Esta deve
   tender a zero: se alguem ainda faz `|~ "timeout"`, falta cobertura de log
   estruturado, e isso e bug de telemetria, nao de habito.
4. **Cobertura de exemplar nas series de erro**, para provar que o caminho
   metrica -> trace existe sem virar label.
5. **Reincidencia do mesmo padrao em servicos diferentes.** Se um mesmo tipo de
   erro aparece em `trace_id` de varios servicos com a mesma raiz, a correlacao
   esta funcionando; se voce ainda precisa adivinhar ordem, falta propagacao.

Criterio de reversao: se a instrumentacao obrigatória atrasar entregas por mais
de um trimestre seguido e a busca por texto continuar resolvendo os incidentes
do time, entao o custo-beneficio desta decisao e negativo para o contexto - e o
substituto correto nao e texto, e `X-Request-Id` como chave unica, mais barato
de propagar.
