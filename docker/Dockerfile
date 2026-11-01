# ClickHouse with Drex's functions: the official server image, plus a Python virtual environment holding nace-sdk,
# drex_udf.py, and the functions' config. The API key is not in the image: mount it read-only at
# /etc/clickhouse-server/drex/nace_api_key (see docker-compose.yml).
ARG CLICKHOUSE_VERSION=26.8.36.3
FROM clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY udf/requirements.txt /opt/drex-clickhouse/requirements.txt
RUN python3 -m venv /opt/drex-clickhouse/venv \
    && /opt/drex-clickhouse/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        -r /opt/drex-clickhouse/requirements.txt

COPY udf/drex_udf.py /opt/drex-clickhouse/drex_udf.py
COPY config/drex_function.xml /etc/clickhouse-server/drex_function.xml
