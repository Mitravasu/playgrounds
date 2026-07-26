# Build arguments allow deliberate upgrades; defaults are the verified multi-platform digests.
ARG NODE_IMAGE=node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69

FROM ${NODE_IMAGE} AS storybook

WORKDIR /opt/storybook-template
COPY storybook-template/package.json storybook-template/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY storybook-template/ ./
ENV STORYBOOK_DISABLE_TELEMETRY=1
RUN npm run typecheck \
    && npm run build-storybook -- --output-dir /tmp/storybook-smoke \
    && rm --recursive --force node_modules/.cache \
    && ln --symbolic /tmp node_modules/.cache

FROM ${PLAYWRIGHT_IMAGE}

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sandbox \
    && mkdir --parents /opt/playgrounds-sandbox /work/input /work/output \
    && pip install --no-cache-dir playwright==1.61.0

COPY app/ /opt/playgrounds-sandbox/
COPY --from=storybook /usr/local/bin/node /usr/local/bin/node
COPY --from=storybook /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=storybook /opt/storybook-template /opt/storybook-template
RUN ln --symbolic /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln --symbolic /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version

ENV HOME=/tmp \
    npm_config_audit=false \
    npm_config_fund=false \
    npm_config_offline=true \
    npm_config_update_notifier=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/playgrounds-sandbox \
    STORYBOOK_DISABLE_TELEMETRY=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config
WORKDIR /work
USER sandbox
