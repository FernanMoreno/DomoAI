# NOT FOR PRODUCTION: disposable client image for the Docker qualification lab.
FROM python:3.12.13-slim-bookworm

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY skills/core ./skills/core
RUN python -m pip install --no-cache-dir .

COPY deploy/multihost/qualification/gateway_fencing_lab.py /opt/domoai-lab/gateway_fencing_lab.py
COPY deploy/multihost/qualification/tls_probe.py /opt/domoai-lab/tls_probe.py
COPY deploy/multihost/qualification/load_generator.py /opt/domoai-lab/load_generator.py
RUN chmod 0555 /opt/domoai-lab/gateway_fencing_lab.py
RUN chmod 0555 /opt/domoai-lab/tls_probe.py /opt/domoai-lab/load_generator.py

# The v2 Compose profile overrides this default with multihost_host.
EXPOSE 8090
LABEL com.domoai.lab.host-agent="multihost_host"
ENTRYPOINT ["python", "-m", "domoai.lab.multihost_qualification"]
