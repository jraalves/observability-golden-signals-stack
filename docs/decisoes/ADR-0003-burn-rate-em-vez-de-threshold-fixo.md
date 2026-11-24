# ADR-0003: burn rate multi-janela em vez de threshold fixo de taxa de erro

Status: aceito
Data: 2026-06-02
Decisao aceita em: 2026-06-09
Decisor: autor (SRE), com o time de plataforma que recebe as paginações
Contexto relacionado: `prometheus/rules/slo-burn-rate.yml`,
`prometheus/rules/golden-signals.yml`, `grafana/dashboards/slo-error-budget.json`,
`runbooks/rb-taxa-erro.md`, `runbooks/rb-latencia.md`, `docs/custos.md`

## Contexto

O stack tem duas famílias de alerta para o mesmo fenômeno. `ErrorRatioHigh`
warning e `ErrorRateCritical` critical disparam sobre uma janela curta e fixa
(`service:http_error_ratio:rate5m` acima de 0,02 e de 0,05). `Slo*BurnRate*`
disparam sobre a razão entre o erro observado e o budget de erro do SLO, em
quatro janelas (1h, 6h, 24h, 14d).

Threshold fixo responde a "a taxa de erro está alta agora". Isso é uma pergunta
diferente de "estamos caminhando para estourar o compromisso do trimestre", que
é a pergunta que decide se alguém acorda às 3h da manhã. As falhas observadas
no método de threshold fixo, todas reproduzíveis em ambiente sintetico:

1. **Falso positivo em volume baixo.** Uma janela de 5m com 3 requests e 1 erro
   produz 33% de taxa de erro. O `expr` do stack fecha isso com o guard
   `service:http_requests:rate5m > 0.1` (ou `> 0.2` nos de saturação), mas o
   guard não resolve o problema de serviço com tráfego sazonal: de madrugada o
   budget estatístico da janela some.
2. **Falso negativo em degradação lenta.** Um serviço que queima 2% do budget
   por dia nunca cruza "5% de erro em 5 minutos" e chega ao fim da janela de 14
   dias com budget esgotado sem ter paginado ninguém.
3. **Threshold sem dono.** Um número como `0.02` não derivado de um objetivo
   não pode ser discutido: a discussão vira opinião sobre o número, em vez de
   discussão sobre o compromisso.
4. **Campanha de silêncio.** Quando o alerta dispara todo dia no mesmo horário e
   ninguém resolve, o time aprende a ignorar o canal. O custo disso é o
   incidente que chega depois, já sem credibilidade.

Burn rate existe para resolver exatamente esses quatro pontos, e o custo é
conhecer o budget antes: `slo:availability:error_budget:ratio =
1 - slo:availability:objective_ratio`, que só existe porque o objetivo vem da
serie `app_slo_availability_ratio` exposta pela propria aplicação.

## Decisao

Adotar **burn rate multi-janela e multi-nível** como mecanismo primário de
paginação, com threshold fixo rebaçado a sinalização de dashbord (warning não
paginável), no seguinte desenho, que é o que está implementado em
`prometheus/rules/slo-burn-rate.yml`:

| Nome | Janelas | Burn rate | Severidade | Efeito no roteamento |
| --- | --- | --- | --- | --- |
| fast | 1h e 5m | > 14,4 | critical | pagina o time dono |
| medium | 6h e 30m | > 6 | critical | pagina o time dono |
| slow | 24h e 2h | > 3 | warning | canal do time, sem paginação |
| budget esgotado | 14d | remaining <= 0 | warning | conversa de objetivo, não incidente |

Regras que acompanham a decisão:

- **Duas janelas por nível, sempre.** A curta evita que um pico de 1 minuto
  pague alguém; a longa evita que uma janela curta com poucos requests decida
  sozinha. Os dois `expr` são combinados com `and on (service, journey)`.
- **Nenhum alerta de negócio sem SLO publicado.** Se `app_slo_availability_ratio`
  não existe para o serviço, o burn rate não existe; o serviço aparece no painel
  de decisão com budget vazio e isso é visível no `slo-error-budget.json`,
  painel 11.
- **Threshold fixo continua existindo**, mas como leitura rápida de incidente
  (`ErrorRatioHigh`, `LatencyP95AboveObjective`), não como fonte primaria de
  páginação noturna.
- O objetivo de disponibilidade do ambiente de exemplo é **0,995 em janela de
  14 dias** e o de latência **0,99 de requests abaixo de 1,2 s**; os dois são
  sinteticos e estão no código (`app/server.py`, `Config`), não em planilha.

## Consequencias

Boas:

- A carga de páginação cai com o volume: 1% de erro constante queima o budget de
  um SLO de 99,5% em cerca de metade da janela, e o alerta `slow` avisa antes.
- O alerta passa a ter semântica de compromisso: "estamos 14x mais rápidos que o
  previsto no consumo do budget" é uma frase que cabe num postmortem.
- O painel de budget (painel 7 e 8 do `slo-error-budget.json`) responde "quantos
  minutos de erro ainda cabem no trimestre" sem consulta manual.

Custosas, assumidas de propósito:

- **Dependência de um número de negócio.** Precisa existir um objetivo acordado,
  com dono e data de revisão. Sem isso, burn rate é threshold fixo com dois
  andares de aritmética.
- **Mais recording rules.** São 4 janelas × 2 SLOs × (razão de erro, burn rate)
  = 16 series adicionais por `(service, journey)`. A conta de cardinalidade está
  em `docs/custos.md`; é barata porque o `by` é fechado.
- **Cauda longa de diagnóstico.** `SloAvailabilityBurnRateSlow` dispara 2h depois
  do começo do problema; para latência isso é tarde, por isso o `LatencyP99Critical`
  de janela curta continua no ar com severidade critical.
- **Risco de duplication com `match`:** o roteamento agrupa por `team` e
  `service`, então um mesmo problema em quatro serviços gera quatro grupos. É o
  comportamento desejado (dono diferente), mas precisa de `group_interval`
  calibrado no Alertmanager para não virar spam.

## Como medir que acertou

1. **Ruído por semana.** Contar páginas por alerta no Alertmanager:
   ```bash
   curl -s 'http://localhost:9093/api/v2/alerts' | python3 -c "import json,sys; \
     print(len(json.load(sys.stdin)))"
   ```
   Criterio: `Slo*BurnRate*` responde por menos de 40% das páginas e por nenhuma
   página repetida do mesmo `(service, journey)` em menos de 4h.
2. **Tempo entre primeiro sinal e página.** Comparar o `startsAt` do alerta
   critico com o primeiro timestamp em que a curva de erro do painel 1 do
   `slo-error-budget.json` sai do ruído. Em ambiente sintetico, com
   `ERROR_RATE` elevado via env, o `fast` deve chegar antes do `slow`.
3. **Budget queimado por incidente.** Registrar no postmortem o valor de
   `slo:availability:error_budget:remaining:ratio14d` no início e no fim. Se a
   soma mensal passar de 100% do budget sem decisão de release travada, o
   objetivo ou o alerting estão errados.
4. **Falso positivo por volume baixo:** contar disparos em que
   `service:http_requests:rate5m` ficou abaixo de 0,5 durante toda a janela. Objetivo
   do projeto: zero, porque o guard `> 0.1` já deveria ter coberto. Se aparecer,
   o guard é que está errado, não o alerta.
5. **Revisão de objetivo.** Uma vez por trimestre, reexecutar a conta do
   `docs/custos.md` e conferir se os SLOs publicados ainda têm dono nomeado no
   arquivo de regras.
