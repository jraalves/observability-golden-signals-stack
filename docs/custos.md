# Custo: como cardinalidade de labels e esquema de tags viram fatura

**Todo número deste documento é sintético.** Não existe tráfego real aqui: as
taxas vêm dos geradores declarados no `docker-compose.yml` (`RATE` de 4, 6 e 2
jornadas/s), a cardinalidade por label vem da tabela `CARDINALIDADE` de
`scripts/custo-cardinalidade.py`, e as constantes de custo por série são ordem de
grandeza do Prometheus com compactação ligada. O objetivo é comparar cenários
entre si - não prever boleto de ninguém.

Reproduzir a conta inteira (a saída abaixo é literal, desta execução):

```bash
python3 scripts/custo-cardinalidade.py          # relatorio
python3 scripts/custo-cardinalidade.py --check  # sai com 1 se estourar o teto
python3 scripts/custo-cardinalidade.py --json   # mesma conta, maquina-legivel
```

## 1. As grandezas que mandam no preço

Prometheus não cobra por volume de request: cobra por **série temporal ativa** e
por **amostra gravada**. As duas são função direta do esquema de labels.

```text
series_ativas = produto das cardinalidades dos labels independentes da familia
amostras/dia  = series_ativas * (86400 / scrape_interval)
RAM_do_head   = series_ativas * 3600 bytes
disco/dia     = amostras/dia * 1,5 bytes
```

`3600 bytes por série no head` e `1,5 byte por amostra` são as constantes de
`scripts/custo-cardinalidade.py`. O que importa é a sensibilidade: dobrando a
contagem de série, dobram RAM e disco juntos, e nenhuma consulta mais inteligente
muda isso.

Existe uma armadilha na primeira linha, e ela custa mais do que a cardinalidade
em si: **label correlato não multiplica**. `service`, `journey`, `team`, `version`
e `owner` andam juntos - um serviço tem uma jornada, um time, uma versão.
Multiplicar os cinco daria `4 * 4 * 4 * 4 * 4 = 1024` identidades para um
ambiente que tem 4. A calculadora por isso trata o grupo de identidade como
**um fator só** (`FATOR_IDENTIDADE = 4`), e multiplicam de verdade apenas os
labels que variam dentro de uma instância: `route`, `status`, `status_class`,
`dependency` e `le`.

O mesmo critério vale para o Loki, que cobra por **bytes ingeridos** e por
**streams indexados**:

```text
streams    = produto das cardinalidades das stream labels (correlatos uma vez)
linhas/dia = requests/s * 86400
dados/dia  = linhas/dia * 380 bytes/linha
indice/dia = linhas/dia * 0,6 byte/linha
```

Superestimar também é erro de custo: quem acredita no produto ingênuo corta sinal
bom para proteger um número que nunca existiu.

## 2. A conta do ambiente de exemplo

Saída literal de `python3 scripts/custo-cardinalidade.py`:

```text
custo de cardinalidade - ambiente local, dados sinteticos
================================================================
trafego sintetico: 18 requests/s  (somando os 3 geradores do docker-compose)
identidade: 4 servicos x 1 instancia(s); labels correlatos contam uma vez
scrape_interval: 15s -> 5760 amostras por serie por dia
[Prometheus] maiores familias de serie, no fleet inteiro:
  http_requests_total                        identidade=4 * environment=1 * route=4 * status=6 * status_class=3 = 288
  http_request_duration_seconds_bucket       identidade=4 * environment=1 * route=4 * le=11             = 176
  dependency_request_duration_seconds_bucket identidade=4 * dependency=2 * le=11                        = 88
  dependency_requests_total                  identidade=4 * dependency=2 * status_class=3               = 24
  http_request_duration_seconds_sum          identidade=4 * environment=1 * route=4                     = 16
  http_request_duration_seconds_count        identidade=4 * environment=1 * route=4                     = 16
  app                            = 692
  recording rules (81 regras) = 940
  metadata de scrape (8 alvos) = 32
  estado de alerta (35 alertas) = 420
  TOTAL DE SERIES                = 2084
  RAM: 2084 * 3600 bytes = 7.2 MB no head
  amostras/dia: 2084 * 5760 = 12003840
  disco: 12003840 * 1.5 bytes = 17.2 MB/dia = 0.23 GB em 14 dias
[Loki]
  streams: 4 reais - 4 servicos, identidade conta uma vez; 64 se a conta ingenua multiplicasse 4 * 1 * 4 * 4
  linhas/dia: 18 * 86400 = 1555200
  dados: 1555200 * 380 bytes = 563.6 MB/dia
  indice: 1555200 * 0.6 bytes = 0.89 MB/dia
  retencao 14 dias = 7902.8 MB
  linhas de /health por dia que o log nao deveria guardar: 17280
[Tempo]
  spans/dia: 18 * 3 * 86400 = 4665600
  dados: 4665600 * 520 bytes = 2313.7 MB/dia
  retencao 2 dias (48h) = 4.52 GB
[cenarios com label proibido, familia http_requests_total]
  route com path completo (query string): identidade=4 * environment=1 * route=250 * status=6 * status_class=3 = 18000 series (antes 288), +17712 no fleet, +0.1 GB de head
  user_id como label de metrica: identidade=4 * environment=1 * route=4 * status=6 * status_class=3 * user_id=5000 = 1440000 series (antes 288), +1439712 no fleet, +4.8 GB de head
  trace_id como label de metrica: identidade=4 * environment=1 * route=4 * status=6 * status_class=3 * trace_id=129600 = 37324800 series (antes 288), +37324512 no fleet, +125.1 GB de head; piso real 129600 (um valor por request)
[teto auto-imposto] series <= 8000, head <= 32.0 MB, disco <= 1.5 GB
resultado: dentro do teto
```

Lendo a conta em voz alta:

- **O custo da app está concentrado em uma família.** `http_requests_total` é
  `4 * 1 * 4 * 6 * 3 = 288` séries - 41% dos 692 que as quatro instâncias de
  aplicação produzem juntas. É também a família que alguém quer "melhorar"
  adicionando label.
- **Histograma é caro por construção.** Cada corte é uma série: `10 buckets +
  le="+Inf" = 11`, daí `4 * 1 * 4 * 11 = 176`. Reduzir para 6 cortes economizaria
  `176 - 96 = 80` séries (3,8% do ambiente) e pioraria a resolução do p95, que é
  o SLI de latência escolhido no README.
- **Recording rule é barata, e é por isso que existem 81 delas.** Somadas, 940
  séries - 45% do total, porque o `by (...)` delas é fechado. Custam disco e
  devolvem consulta barata nos painéis e uma definição única de SLI para alerta e
  painel.
- **Loki é o item mais caro deste ambiente, e não é por índice.**
  `1555200 * 380 = 563,6 MB/dia` de dado ingerido contra 0,89 MB/dia de índice. O
  índice pequeno existe *porque* o corpo do log não é label de stream - decisão do
  `ADR-0004`, não sorteio.
- **Tempo é o segundo maior, e a retenção curta é o que segura.** `18 * 3 = 54`
  spans/s, `54 * 86400 = 4665600` spans/dia, `4665600 * 520 = 2314 MB/dia`. Com os
  `48h` de `compaction.block_retention` do `tempo/tempo.yaml` dá 4,52 GB. Retenção
  de trace igual à de log (14 dias) seria `2313,7 * 14 = 32,4 GB`: 7x os 4,52 GB
  atuais, e 141x o Prometheus inteiro.
- **Série viva não é série máxima.** O smoke-test, com uma jornada só e
  `le`/`status` parcialmente exercitados, mostra ~70 linhas de série no
  `/metrics` de uma instância. Os 288 são o teto do espaço de labels: a conta é de
  capacidade, não de amostra de um momento.

## 3. O que cada label proibido custaria

Os três cenários abaixo são os que aparecem em revisão de código de verdade, e os
três estão em `FORBIDDEN_METRIC_LABELS` de `app/tagging.py`. A família atacada é
`http_requests_total` (288 séries), a mais cara do ambiente.

| Cenário | Conta | Séries | Head | Comparação |
| --- | --- | --- | --- | --- |
| Esquema atual (ADR-0004) | `4 * 1 * 4 * 6 * 3` | 288 | 1,0 MB | - |
| `route` com path completo | `4 * 1 * 250 * 6 * 3` | 18000 | 61,8 MB | 62,5x a família, 8,6x o ambiente |
| `user_id` como label de métrica | `4 * 1 * 4 * 6 * 3 * 5000` | 1440000 | 4,8 GB | 691x o ambiente inteiro |
| `trace_id` como label de métrica | `4 * 1 * 4 * 6 * 3 * 129600` | 37324800 | 125,1 GB | 17910x o ambiente inteiro |

O caso `trace_id` merece a aritmética escrita, porque a proposta parece inocente
("só quero achar o request na métrica"):

```text
requests/s:                 18  (os tres geradores do compose somados)
janela do head:             7200 s (2 h de serie viva em memoria)
valores na janela:          18 * 7200 = 129600
produto com a familia:      4 * 1 * 4 * 6 * 3 * 129600 = 37324800   (limite superior)
piso da conta:              129600 series, ou 129600 * 3600 = 445 MB
```

O produto é um limite superior - cada trace tem uma rota e um status só, então o
número real fica entre 129.600 e 37,3 milhões. **Os dois são inaceitáveis**: o
piso sozinho já é 62x o ambiente inteiro, e o que mata primeiro não é o volume de
disco, é a taxa de criação de série nova: `18` séries por segundo nascendo, 1080 por
minuto, sem que nenhuma deixe de existir. Nenhum ajuste de hardware resolve: não é problema de
velocidade, é problema de contagem.

A resposta correta é estrutural e está no `ADR-0002` e no `ADR-0004` - `trace_id`
vive **no corpo do log** (extraído pelo parser e linkado pelo `derivedFields` do
Grafana) e **como atributo do span** (consultado com TraceQL no Tempo). A métrica
fica agregada e barata, e o request individual continua achável:
`logcli -q '{service="checkout-api"} | json | trace_id="..." '`.

O mesmo critério vale para `route` com path instanciado: a rota normalizada tem 4
valores porque o contrato é o template (`/pay`, `/auth`, `/health`, `unmatched`),
não a URL com parâmetro. `labelize()` em `app/tagging.py` existe para isso, e o
`62,5x` da tabela é o preço de esquecer de chamá-lo.

## 4. Onde a alavanca é grande

Calculadas com as mesmas constantes do item 1:

| Alavanca | Efeito na conta | O que se perde |
| --- | --- | --- |
| `scrape_interval` de 15s para 30s | 5760 amostras/serie/dia -> 2880: `17,2 MB/dia -> 8,6 MB/dia`; retenção `0,23 GB -> 0,12 GB` | resolução em janela curta; o burn rate `fast` de 5m fica com metade dos pontos |
| `scrape_interval` de 15s para 60s | `17,2 MB/dia -> 4,3 MB/dia` | `rate(...[5m])` passa a ter 5 pontos; histograma serrilhado |
| Fechar o vocabulário de `journey` em 4 valores | mantém `4 * 1 = 4` combinações de identidade em vez de texto livre | jornada nova exige PR no schema - que é o objetivo |
| Cortar buckets de 11 para 6 cortes | `176 - 96 = 80` séries a menos (3,8% do ambiente) | resolução do p95 entre 0,5s e 1,2s, onde mora o objetivo de latência |
| Retenção do Prometheus de 30d para 14d | `0,50 GB -> 0,23 GB` | histórico de tendência de longo prazo |
| `table_manager.retention_period` do Loki de 14d para 10d | `7903 MB -> 5645 MB` | alcance de investigação de incidente antigo |
| Retenção do Tempo de 48h para 24h | `4,52 GB -> 2,26 GB` | reconstrução de incidente do dia anterior |
| Não mandar `/health` para o Loki | `4 * (86400 / 20) = 17280` linhas/dia, `17280 * 380 = 6,3 MB/dia` | nada: vivacidade já está na métrica e no healthcheck do container |

As quatro primeiras linhas trocam **qualidade de sinal** por espaço e por isso são
decisão de time com registro; as quatro últimas trocam **conveniência** por espaço
e deveriam ser default. Note a desproporção: o `trace_id` como label de métrica
custa 125 GB de head, enquanto todo o resto do ambiente custa 7,2 MB. É por isso
que a discussão de schema é mais rentável que a discussão de hardware.

## 5. Regras práticas que este repositório segue

1. **Label novo é mudança em `app/tagging.py`.** Se não está no schema, não vira
   label de métrica nem de stream. `cardinality_risk()` classifica cada chave e
   `tests/test_tagging.py` trava a entrada de item proibido.
2. **Antes de adicionar um label, multiplique.** A pergunta é `series_da_familia *
   valores_novos_do_label = ?`, respeitando correlação. Se o resultado passa do
   teto auto-imposto (8000 séries, 32 MB de head, 1,5 GB de disco na retenção), o
   label não entra. O teto é verificado no CI com `--check`.
3. **Alto cardinalidade vai para log e span, não para métrica.** `request_id` e
   `trace_id` são campos de log; `user_id` é atributo de span quando necessário.
4. **A esteira de telemetria é um serviço, não um consumo.** Collector, Loki e
   Tempo têm limites próprios (`max_line_size`, `ingestion_rate_mb`,
   `max_traces_per_user`, `compaction.block_retention`), todos explícitos nos
   arquivos de configuração - porque o custo que não é declarado é o custo que
   aparece de madrugada.
5. **Custo de índice de trace se controla por atributo indexado, não por
   amostragem às cegas.** TraceQL filtra por atributo; o que encarece é indexar
   tudo, conclusão conjunta de `ADR-0001` e `ADR-0004`.
6. **Durante rollout, o label `version` dobra.** Duas versões convivendo por
   algumas horas multiplicam a identidade por 2 - o efeito é temporário, mas é
   exatamente o momento em que o painel de saturação do Prometheus mostra degrau.
   Não confunda com vazamento.

## 6. Como conferir no ambiente de pé

```bash
# series ativas reais, nao a estimativa da calculadora
promtool query instant http://localhost:9090 'prometheus_tsdb_head_series'
promtool query instant http://localhost:9090 \
  'sum by (job) (prometheus_tsdb_head_series)'

# quantos valores cada label tem de verdade - a pergunta que define o proximo mes
curl -s 'http://localhost:9090/api/v1/label/route/values' | head -c 400
promtool query instant http://localhost:9090 \
  'count({__name__="http_requests_total"}) by (route)'

# custo de criação de serie nova: o numero que antecede qualquer incidente de head
promtool query instant http://localhost:9090 \
  'rate(prometheus_tsdb_head_series_created_total[15m])'

# streams do Loki: cada stream novo e custo de indice
curl -s 'http://localhost:3100/loki/api/v1/label/service/values' | head -c 400
logcli -q '{service="checkout-api"}' --limit 1
```

Para a app de exemplo, o smoke-test também responde parte da pergunta sem Docker:
`bash scripts/smoke-test.sh` mostra quantas linhas de série o `/metrics` entrega
depois de 50 journeys sintéticas.

Se `prometheus_tsdb_head_series` divergir da estimativa da calculadora em mais de
2x, a diferença é label entrando sem revisão - e o ponto de correção é o schema em
`app/tagging.py`, não o limite de amostras do Prometheus.
