# The build command supplies a digest-pinned Playwright image, for example:
# mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:<resolved-digest>
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.61.0-noble
FROM ${PLAYWRIGHT_IMAGE}

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sandbox \
    && mkdir --parents /opt/playgrounds-sandbox /work/input /work/output \
    && pip install --no-cache-dir playwright==1.61.0

COPY app/ /opt/playgrounds-sandbox/

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/playgrounds-sandbox \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config
WORKDIR /work
USER sandbox
