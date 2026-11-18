# nota-aberta no cluster k3s (sistema de apps/sistemas, imagem publicada pelo cd-imagens-k3s.yml).
#
# 1º estágio: compila o motor OFICIAL da Receita Federal (MIT) num commit fixo, sem tocar no código
# dele, e gera o SQLite das tabelas pelo flyway deles. Atualizar o motor = trocar RTC_COMMIT (e a
# versão em README.md) num PR; o teste de paridade (tests/, RTC_MOTOR_REAL) roda contra o jar novo.
# 2º estágio: JRE + python3 para a porta (servidor.py; o python3-minimal não traz json nem http.server). Raiz só leitura: o banco é aberto em modo
# ro pelo próprio perfil `offline` do motor; o Java escreve só em /tmp.
#
#   docker build -t nota-aberta apps/sistemas/nota-aberta
#   docker run --rm -p 8080:8080 --read-only --tmpfs /tmp nota-aberta
FROM maven:3.9-eclipse-temurin-21@sha256:99e61abcff91a9b1333463bd8451fb18495d6eba9250ac66a338b518f8278320 AS motor
ARG RTC_REPO=https://github.com/ReceitaFederal/calculadora-rtc-regime-regular.git
# Commit registrado na Portaria Cotec nº 342/2026 (DOU de 13/11/2026, art. 2º, § 1º, I, b) como a versão
# inicial publicada (1.6.1). Trocar por outro exige rodar a paridade; tests/ confere o valor.
ARG RTC_COMMIT=150cccbe414d4a5edea703e4f7a424af5aa222c7
WORKDIR /src
RUN git init -q . && git remote add origin "$RTC_REPO" \
 && git fetch -q --depth 1 origin "$RTC_COMMIT" && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$RTC_COMMIT"
RUN mkdir -p calculadora/db \
 && mvn -q -B flyway:migrate -Dflyway.configFiles=./flyway/flyway-pro.conf \
 && mvn -q -B -DskipTests package \
 && cp target/api-regime-geral.jar /src/motor.jar
# O driver SQLite extrai a biblioteca nativa em /tmp na partida, e /tmp montado `noexec` (o
# `--tmpfs` do Docker é assim) dá UnsatisfiedLinkError e o motor não sobe. Extraída aqui, ela fica
# na raiz só leitura e o driver a carrega por org.sqlite.lib.path (RTC_JAVA_OPTS).
RUN mkdir -p /tmp/x /src/native && cd /tmp/x \
 && LIB=$(jar tf /src/motor.jar | grep -m1 'BOOT-INF/lib/sqlite-jdbc-.*\.jar$') \
 && jar xf /src/motor.jar "$LIB" && jar xf "$LIB" "org/sqlite/native/Linux/$(uname -m)/libsqlitejdbc.so" \
 && cp "org/sqlite/native/Linux/$(uname -m)/libsqlitejdbc.so" /src/native/ && rm -rf /tmp/x

FROM eclipse-temurin:21-jre-noble@sha256:0d57a8e90c2bb844c5a8e1b2b71e11eff06dad7edcfd803cd21ed4ae203b2515
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 \
    RTC_JAR=/rtc/motor.jar RTC_DIR=/rtc \
    RTC_JAVA_OPTS="-XX:+UseSerialGC -Xmx160m -Xss512k -XX:MaxMetaspaceSize=160m -XX:ReservedCodeCacheSize=48m -XX:TieredStopAtLevel=1 -Djava.io.tmpdir=/tmp -Dorg.sqlite.lib.path=/rtc/native -Dorg.sqlite.lib.name=libsqlitejdbc.so"
COPY --from=motor /src/motor.jar /rtc/motor.jar
COPY --from=motor /src/calculadora/db/calculadora-pro.db /rtc/calculadora/db/calculadora-pro.db
COPY --from=motor /src/native/libsqlitejdbc.so /rtc/native/libsqlitejdbc.so
COPY --from=motor /src/LICENSE /rtc/LICENSE-receita-federal
WORKDIR /app
COPY servidor.py mcp.py sistema.json ./
COPY web ./web
USER 10001
EXPOSE 8080
CMD ["python3", "servidor.py"]
