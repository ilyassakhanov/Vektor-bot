# syntax=docker/dockerfile:1
# Vektor bot — hardened runtime image.
#
# The container is the host boundary: model-chosen exec commands, the MCP
# CVE server and the bot itself run as an unprivileged user on a read-only
# root filesystem (see compose.yaml for the runtime flags).

ARG PYTHON_IMAGE=python:3.14-slim-trixie

# --- builder: resolve wheels into a self-contained venv --------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

# --- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE}

# Network read-only tools for the exec allowlist (EXEC_ALLOWED_COMMANDS
# default: curl, dig, nslookup, whois, host). All land in /usr/bin, which is
# on the exec tool's fixed safe PATH.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl bind9-dnsutils bind9-host whois \
    && rm -rf /var/lib/apt/lists/*

# Process-control tools are removed so even a widened allowlist cannot
# reproduce the "ps | grep python; kill -9 <bot>" incident. The build fails
# if any of them still exists as a file on the safe PATH. (A file check, not
# `command -v`: `kill` is a dash builtin, so that would always succeed.)
RUN for b in ps kill pkill pgrep killall top skill snice; do \
        for d in /usr/bin /bin /usr/sbin /sbin; do rm -f "$d/$b"; done; \
    done \
    && for b in ps kill pkill pgrep killall top; do \
        for d in /usr/bin /bin /usr/sbin /sbin; do \
            test ! -e "$d/$b" \
                || { echo "process-control binary still present: $d/$b" >&2; exit 1; }; \
        done; \
    done \
    && for b in curl dig nslookup whois host; do \
        test -x "/usr/bin/$b" || { echo "missing exec binary: $b" >&2; exit 1; }; \
    done

RUN groupadd --system --gid 10001 vektor \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin vektor \
    && mkdir -p /data \
    && chown 10001:10001 /data \
    && chmod 0750 /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Code is owned by root and read-only for the bot user.
COPY --chmod=0644 *.py ./
COPY agent/ ./agent/
COPY llm/ ./llm/
COPY tools/ ./tools/
COPY retrieval/ ./retrieval/
COPY mcp_servers/ ./mcp_servers/
COPY skills/ ./skills/

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    KB_DB_PATH=/data/vektor.db \
    METRICS_PORT=9101

USER 10001:10001

# The metrics server starts first in main(), so a reachable /metrics means
# the process is alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/metrics' % os.environ.get('METRICS_PORT', '9101'), timeout=4)"]

CMD ["python", "bot.py"]
