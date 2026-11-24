# Contribuindo

Este repositório é material de estudo de observabilidade com stack open source.
Ele aceita três tipos de contribuição, e a barra é a mesma para os três: **o
`scripts/validate.py` continua saindo com código zero, os testes passam e o
smoke-test roda de ponta a ponta.**

```bash
python3 scripts/validate.py                  # contrato estrutural do repositorio
python3 -m unittest discover -s tests -v     # testes, sem rede
bash scripts/smoke-test.sh                   # app + carga + saude + traco no log
```

## Antes de começar

Pré-requisito único: Python 3.10+ (o código usa `X | None` e `dict[str, ...]` no
tipo). Nem `pip install` é necessário: a app de exemplo é stdlib apenas, e o
requisito é verificado pelo validador - se `app/requirements.txt` ganhar uma
linha que não seja comentário, o build falha. Docker é opcional, necessário só
para o `docker compose up` completo (o smoke-test não precisa dele).

## O contrato que este repositório mantém

| Coisa | Onde mora | Quem cobra |
| --- | --- | --- |
| Estrutura das regras do Prometheus (`groups[].rules[]`, `for`, `labels.severity`, `labels.team`, `labels.signal`, `annotations.runbook_url`) | `prometheus/rules/*.yml` | `validate.py` + `tests/test_rules_structure.py` |
| Todo alerta aponta para um runbook que existe e aparece na matriz | `runbooks/*.md`, `runbooks/index.md` | `validate.py` |
| Esquema de tags (`REQUIRED_TAGS`, `FORBIDDEN_METRIC_LABELS`, vocabulário de `journey`) | `app/tagging.py` | `tests/test_tagging.py`, `ADR-0004` |
| Métricas e SLOs expostos pela app | `app/server.py` | `tests/test_app_telemetry.py`, scraping do compose |
| Formato do log com `trace_id` | `app/server.py`, `Logger.emit` | `tests/test_app_telemetry.py`, `scripts/smoke-test.sh` |
| Datasheet dos painéis (uid estável, `gridPos` sem sobreposição, datasource conhecido) | `grafana/dashboards/*.json` | `validate.py` |
| Nada de dado real, nada de credencial, nada de nome de ambiente próprio | todo arquivo de texto | varredura de termos protegidos do `validate.py` |

## Como adicionar um alerta

Um alerta novo só entra com o pacote completo. A ordem abaixo é a ordem em que
faz diferença:

1. **Defina o SLI antes da expressão.** Escreva em uma frase o que o alerta
   observa e o que ele não observa. Exemplo do que já existe:
   `ErrorRatioHigh` observa a razão 5xx/total em 5m com guard de volume; ele
   **não** observa indisponibilidade de dependência - essa é a função de
   `DependencyErrorRatioHigh`.
2. **Publique a métrica derivada como recording rule**, não dentro do `expr` do
   alerta. A gravação é o que o painel e o alerta consomem juntos; duplicar a
   expressão é como se começa a ter dois p95 diferentes no mesmo repositório.
3. **Rótulos obrigatórios:** `severity` (`info`, `warning` ou `critical`), `team`
   (usado no `group_by` do Alertmanager) e `signal` (`latency`, `traffic`,
   `errors`, `saturation`, `slo` ou `infrastructure`). O `annotations.summary`
   descreve o sintoma; `annotations.description` dá a primeira hipótese.
4. **`for:` sempre.** Sem período de confirmação, o alerta pagina com o jitter do
   scrape. O validador recusa regra de alerta sem `for`.
5. **`annotations.runbook_url` apontando para um arquivo que existe**, com as
   seis seções obrigatórias (`## Gatilho`, `## Hipoteses em ordem de custo de
   verificacao`, `## Diagnostico`, `## Acao`, `## Criterio de saida`, `## O que
   aprender depois`), ao menos três ferramentas de diagnóstico reais citadas e a
   linha de aprovação humana para ação destrutiva.
6. **Linha na matriz de `runbooks/index.md`**: coluna Alerta, Severity, Signal,
   Runbook, Painel e Primeiro comando. Sem a linha, o alerta é considerado
   inatingível na hora do incidente e o validador reclama.
7. **Painel correspondente**, se o alerta existe para ser olhado em gráfico. Sem
   sobreposição de `gridPos` e com `datasource.uid` dos declarados em
   `grafana/provisioning/datasources/datasources.yml`.

## Como mudar a definição de "request bom"

Este é o único tipo de mudança que exige revisão de SRE explicitamente pedida.
O contrato atual é:

- request bom = `status_class != "5xx"`;
- **4xx não queima budget de erro** (validação de entrada do usuário não é
  indisponibilidade do serviço);
- `/health` fica **fora** do SLI, porque mede vivacidade de processo e contá-lo
  dilui o sinal do comportamento do usuário;
- a janela de latência usa `le="1.2"` como corte de conformidade, o mesmo número
  de `slo:latency:objective_seconds`.

Mexer em qualquer um desses quatro pontos muda todos os SLOs, todos os burn rates
de `prometheus/rules/slo-burn-rate.yml`, os painéis de budget e a redação dos
runbooks de erro e latência de uma vez. Se você precisa disso, abra uma issue
**antes** do PR, e trate como sucessão de decisão registrada em `ADR-0003`.

## Como adicionar ou remover um label

Labels são custo (ver `docs/custos.md`) e são contrato (ver `ADR-0004`). O
procedimento é:

1. mexer em `app/tagging.py` (`REQUIRED_TAGS`, `OPTIONAL_TAGS`, `JOURNEYS`,
   `FORBIDDEN_METRIC_LABELS`) e rodar `python3 -m unittest discover -s tests`;
2. atualizar `otel/tagging.md` e o processor `attributes` de
   `otel/collector-config.yaml` no mesmo commit - os dois lados têm de falar a
   mesma língua;
3. atualizar `CARDINALIDADE` e `FAMILIAS_APP` de
   `scripts/custo-cardinalidade.py` e colar a nova saída em `docs/custos.md`;
4. conferir se alguma recording rule ou painel depende do label removido:
   ```bash
   grep -rn "nome_do_label" prometheus/ grafana/ runbooks/
   ```
5. justificar no PR qual pergunta de negócio o label responde. "Pode ser útil um
   dia" não é resposta: cada valor novo é uma série na memória do Prometheus e um
   stream no Loki.

## Convenções de escrita

- Documentação em português, sem emoji, sem nome de cliente, de empregador ou de
  ambiente real. Termos técnicos de produto (Prometheus, Grafana, OTel) entram
  porque são software livre publicado.
- Código comentado explicando **por que**, não o que a linha faz. Comentários do
  tipo `# incrementa o contador` são removidos na revisão.
- Toda configuração com número tem unidade no nome da chave ou no comentário ao
  lado (`48h`, `1.2` s, `15s`, `380` bytes).
- Nada de segredo em arquivo versionado, mesmo placeholder com formato crível:
  use `valueFrom`/secret manager e diga isso no `app/otel-sidecar.md`.
- Toda saída de benchmark, latência ou contagem em documento precisa vir da
  execução de um script deste repositório, com o comando indicado junto.

## Testes

`tests/` é `unittest` puro, sem pytest, sem rede, sem container:

```bash
python3 -m unittest discover -s tests -v
```

Cobertura pedida: estrutura das regras de alerta, o esquema de tags, o cálculo de
p95 e de taxa de erro da app, e a presença de `trace_id` no log emitido pelo
formato da aplicação. Se você mudar comportamento do `app/stats.py` ou do
`app/tagging.py`, o teste correspondente muda junto no mesmo commit.

## Pipeline

`.github/workflows/ci.yml` e `.gitlab-ci.yml` rodam a mesma coisa, com os mesmos
passos e sem nenhum segredo: `validate.py`, `py_compile`, `unittest discover` e
`smoke-test.sh`. Se um dos dois arquivos divergir, o validador não percebe - o
revisor percebe. Mude os dois.

## Ciclo de revisão

1. Abra um PR pequeno; regra de alerta e runbook vão juntos no mesmo PR.
2. CI verde com os quatro passos acima.
3. Descreva no PR **qual pergunta o alerta ou painel responde** e **o que você
   rodou para conferir**, colando a saída do comando.
4. Mudança de schema de tag, de definição de SLI ou de política de roteamento do
   Alertmanager pede ADR novo (copie o esqueleto de `docs/decisoes/`), com as
   quatro seções obrigatórias que o validador cobra.

Dúvida de escopo? A régua do projeto é uma frase: **isto ajuda alguém a operar o
stack num sábado à noite?** Se não ajuda, é documentação de produto, não deste
repositório.
