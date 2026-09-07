# NOT FOR PRODUCTION: local disposable Patroni image for qualification exercises.
# It carries no production credentials, TLS material, backup policy, or fault isolation.
FROM postgres:16.4-bookworm

LABEL com.domoai.lab="true" \
      com.domoai.not-for-production="true"

RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/patroni \
    && /opt/patroni/bin/python -m pip install --no-cache-dir "patroni[etcd3]==3.3.8" "psycopg[binary]==3.2.3" \
    && mkdir -p /etc/patroni \
    && chown -R postgres:postgres /etc/patroni /opt/patroni

COPY --chown=postgres:postgres patroni.yml /etc/patroni/patroni.yml

USER postgres
CMD ["/opt/patroni/bin/patroni", "/etc/patroni/patroni.yml"]
