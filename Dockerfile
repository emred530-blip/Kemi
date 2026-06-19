# Kemi — a containerised fleet node.
#   docker build -t kemi .
#   docker run --rm -p 7700:7700/tcp -p 7700:7700/udp -p 8080:8080 kemi
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY kemi ./kemi
RUN pip install --no-cache-dir ".[crypto]"

# tcp+udp share the node port; the dashboard is separate
EXPOSE 7700/tcp 7700/udp 8080/tcp

# persist identity, ledger and reputation across restarts
VOLUME /data

# default: a provider with the dashboard on 0.0.0.0 (override the CMD freely)
CMD ["sh", "-c", "kemi node --provide --port 7700 \
     --identity /data/identity.json --ledger /data/ledger.db \
     --reputation /data/reputation.db --ui 8080 --ui-host 0.0.0.0"]
