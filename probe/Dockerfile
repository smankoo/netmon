FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY probe ./probe

ENV OUTBOX_PATH=/data/outbox.db
VOLUME ["/data"]

CMD ["python", "-m", "probe.runner"]
