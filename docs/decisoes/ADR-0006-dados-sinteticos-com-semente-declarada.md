# ADR-0006: dados sintéticos com semente declarada em vez de dado de produção

Status: aceito
Data: 2026-07-20
Decisao aceita em: 2026-07-27
Decisor: autor (SRE)
Contexto relacionado: `README.md`, `SECURITY.md`, `app/server.py`,
`app/loadgen.py`, `prometheus/rules/golden-signals.yml`,
`runbooks/index.md`, `docs/custos.md`

## Contexto

Este repositório documenta como operar um stack de observabilidade e como
escrever runbooks que funcionam. Para isso ele precisa de dado: histogramas com
cauda, séries de taxa de erro, logs com `trace_id`, traces com span de
dependência lento. As alternativas para obter esse dado são:

1. **Espelhar produção.** Produz o sinal mais fiel e é inaceitável: log de
   aplicação real contém identificador de usuário, URL com parâmetro, mensagem
   de erro com dado de cliente. Um repositório público com esse conteúdo é
   incidente de privacidade, não material de estudo.
2. **Não publicar dado, só configuração.** Barato e inútil: painel sem série é
   indistinguível de painel quebrado, e o valor do projeto está em ver que o
   alerta dispara com o trafego sintético ligado.
3. **Gerar dado sintético com processo determinístico e rotulado.** O que este
   ADR escolhe.

Existe um segundo motivo, técnico: um número inventado em documento é
indistinguível de um número medido. Se o README afirmar "p95 de 340 ms" sem
origem, o leitor não tem como saber se aquilo veio de execução ou de
imaginação - e ele vai levar para o ambiente dele.

## Decisao

- **Todo dado gerado por este repositório é sintético** e rotulado como tal em
  três lugares independentes: no código (`Config.version` termina em
  `-sintetico`, `environment` é `local-sintetico`, a env `DADOS=sinteticos` no
  `docker-compose.yml`), nos logs (`loadgen.py` grava `sintetico: true` em cada
  linha) e na documentação (aviso na abertura do README, dos runbooks e deste
  arquivo).
- **Toda aleatoriedade tem semente declarada.** A app usa `SEED=2026`
  (ou o `--seed` da linha de comando) e o gerador de carga usa `SEED=1337`. As
  séries resultantes são reprodutíveis: mesma semente, mesma sequência de
  `trace_id`, mesma distribuição de desfechos.
- **Nenhum número de desempenho apresentado como medido vem de documento.** Os
  limiares publicados (`0,02` de razão de erro, `1,2 s` de latência, `0,995` de
  objetivo) são parâmetros de política sintéticos, escolhidos como ponto de
  partida de discussão, e é assim que aparecem no README. Resultado de execução
  (contagem de linhas de log com `trace_id`, contagem de alertas, contagem de
  painéis) só aparece em relatório acompanhado da saída do comando que o gerou.
- **As dependências simuladas são ficcionais**: `gateway-pagamentos` e
  `provedor-identity` são nomes de papel. Não há chamada externa, não há
  provedor real, não há cliente real representado.

## Consequencias

Boas:

- Qualquer pessoa consegue reproduzir o comportamento do stack e ver o alerta
  disparar em minutos, com `LATENCY_FACTOR` e `ERROR_RATE` controláveis por env.
- O repositório pode ser publicado sem risco de vazamento de dado de terceiros,
  e o `SECURITY.md` fica curto por construção: não há segredo para reportar.
- Testes ficam determinísticos: `p95` esperado de uma lista fixa é uma
  asserção válida, e o `tests/test_stats.py` depende disso.

Custosas, declaradas:

- **Não é benchmark.** A latência da app de exemplo é `base_ms` mais jitter com
  semente; ela não representa hardware real, nem concorrência de verdade, nem o
  custo de um cliente HTTP de verdade. Números de atraso daqui não devem ser
  citados como capacidade de nada.
- **Distribuição limpa demais.** Dado sintético não tem cardinalidade caótica,
  tráfego de robô, nem picos de campanha. Alertas calibrados aqui tendem a
  parecer bons demais; a calibração em ambiente real precisa ser refeita.
- Dá trabalho rotular: cada novo script precisa lembrar de escrever
  `sintetico`/`sinteticos` em algum campo visível. O `tests/test_support_files.py`
  cobre o README e os runbooks; para script novo, a revisão de PR cobre.

## Como medir que acertou

1. **Reprodutibilidade:** rodar duas vezes com a mesma semente e comparar a
   sequência de `trace_id` do log:
   ```bash
   python3 app/server.py --port 18091 --seed 2026 --log-dir /tmp/a &
   python3 app/server.py --port 18092 --seed 2026 --log-dir /tmp/b &
   ```
   depois extrair `trace_id` em ordem dos dois arquivos e conferir igualdade.
   Sucesso: hashes idênticos para a mesma quantidade de requests.
2. **Rastreabilidade de rótulo:** `grep -ril sintetico` no repositório deve
   cobrir README, `docker-compose.yml`, `app/server.py`, `app/loadgen.py` e os
   runbooks. Se um arquivo novo de dados aparecer sem rótulo, a decisão foi
   violada.
3. **Nada de produção:** o validador estrutural mantém a varredura de termos
   protegidos de todo arquivo de texto do repositório com zero ocorrências
   (`python3 scripts/validate.py`).
4. **Custo pedagógico:** medir quantos minutos levam, do clone ao primeiro alerta
   visível, em máquina sem acesso a índice de pacote. Se aparecer dependência
   externa no caminho, é sinal de que a rota escolhida aqui está sendo traída
   pelo conforto.
