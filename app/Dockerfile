# Aplicacao de exemplo: Python 3.12 + stdlib. Nenhuma camada de dependencias.
#
# A imagem e a mesma para os quatro servicos do docker-compose; o que muda e o
# conjunto de env vars (SERVICE/JOURNEY/TEAM/ERROR_RATE/LATENCY_FACTOR/SEED).
# Construir uma imagem por servico seria desperdicio e enganaria quem lesse o
# compose como "quatro aplicacoes diferentes".
FROM docker.io/library/python:3.12-slim

# -u: log descompensado para o stdout chegar ao Loki sem buffer de 4KB.
# -B: sem .pyc em disco, imagem e container efemero.
# PYTHONPATH: os modulos sao planos (server.py importa stats/tagging do mesmo
# diretorio), entao nao ha pacote a instalar.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    PORT=8080

LABEL org.opencontainers.image.title="obs-stack sample-app" \
      org.opencontainers.image.description="App de exemplo com golden signals, traceparent e log JSON; dados sintetico" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /srv/app

# Copia explicita em vez de COPY .: assim o build falha se alguem esquecer de
# declarar um modulo novo, em vez de empacotar por acidente.
COPY server.py stats.py tagging.py loadgen.py requirements.txt ./

# Usuario nao root criado com UID fixo: em Kubernetes com securityContext
# (runAsNonRoot) um UID aleatorio de imagem quebraria o pod.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --home-dir /srv/app --shell /usr/sbin/nologin app \
    && mkdir -p /var/log/app \
    && chown -R app:app /srv/app /var/log/app

USER app

# /var/log/app e o caminho que o receiver filelog do Collector le (montado como
# volume compartilhado no docker-compose). O default de LOG_DIR aponta para ali,
# mas continua configuravel para rodar fora do container.
ENV LOG_DIR=/var/log/app
EXPOSE 8080

# Healthcheck de imagem: usa a propria app, sem curl na base slim. O
# docker-compose sobrescreve este test com o hostname do servico, que resolve
# melhor dentro da rede do que localhost em algumas configuracoes de bind.
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python3", "-c", "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=3)"]

# Sem `pip install` aqui: requirements.txt e documentado como vazio de
# proposito. Se uma dependencia third-party aparecer, este build passa a ser o
# lugar de instala-la - e o ADR correspondente aparece junto.
ENTRYPOINT ["python3", "/srv/app/server.py"]
