# Playgrounds

Analyze a website's rendered design system and generate a matching React
Storybook inside isolated browser sandboxes.

## Development

Install the project and development dependencies:

```bash
make setup
```

Configure Ollama Cloud:

```bash
cp .env.example .env
```

Replace the placeholder `OLLAMA_API_KEY` in `.env`. The defaults use
`https://ollama.com` and `gemma4:cloud`.

Run all checks:

```bash
make check
```

Build the local browser image and recreate the analyzer proxy, then analyze a
public HTTPS page:

```bash
make analyzer-up
uv run playgrounds analyze https://www.mitravasu.com/
```

The command writes validated evidence and a synthesized `style-guide.json` to
`runs/<run-id>/analysis/`.

Generate a Storybook manually from that run:

```bash
uv run playgrounds create run_<id> "Create a dropdown with three account actions."
```

The same commands are available through Make:

```bash
make analyzer URL=https://www.mitravasu.com/
make creator RUN=run_<id> PROMPT="Create a dropdown with three account actions."
```

The creator first produces a validated component and story plan from the source
screenshot and style guide. A second small call generates shared CSS tokens.
Then, up to four isolated component calls run concurrently. Each receives only
its own plan and stories, the style guide, and the shared tokens, and returns
exactly one TSX, CSS, and CSF story file. Components cannot import one another.
The trusted host assembles and validates the complete project.

Models cannot generate package configuration, dependencies, or build commands;
the image-owned Storybook template supplies that boilerplate. Phase-specific raw
responses remain under each attempt as `plan.*`, `tokens.*`, and
`component-<Name>.*`.

Creator model calls log their prompt and image sizes, emit a waiting heartbeat
every 15 seconds, and report wall-clock duration when they finish. When Ollama
provides server metrics, the completion log also includes prompt and output token
counts, evaluation durations, and output tokens per second. Model HTTP operations
fail after `OLLAMA_TIMEOUT_SECONDS` (ten minutes by default) instead of waiting
indefinitely. Planning has a separate two-minute
`OLLAMA_PLANNING_TIMEOUT_SECONDS` deadline and falls back to a minimal host plan
if it expires.

Ollama Cloud does not support its structured-output `format` field, so
`OLLAMA_STRUCTURED_OUTPUTS` defaults to `false`. The host still validates every
JSON response with strict Pydantic models and makes one bounded repair request.
Set the option to `true` only for a compatible self-hosted Ollama server.

The offline sandbox type-checks the project, builds a static Storybook, discovers
its story index, and renders every declared story in Chromium at its desktop or
mobile viewport. It records per-story browser, network, render, and baseline
accessibility facts. A fresh reviewer scores the valid result along eight
dimensions:

- design-language adherence;
- contextual appropriateness;
- interaction and state quality;
- responsive behavior;
- accessibility beyond the hard gates;
- design-system coherence;
- story coverage and documentation;
- implementation quality.

The creator makes at most one complete-project revision. The selected source,
static build, screenshot, diagnostics, metadata, and evaluation are written to
`storybooks/<creation-id>/`. Full attempt history remains under the analyzer run.

```text
storybooks/<creation-id>/
├── project/             # Generated source
├── storybook-static/    # Directly openable static build
├── project.json
├── storybook.zip
├── screenshot.png
├── render.json
├── evaluation.json
└── metadata.json
```

## Offline Storybook toolchain

The sandbox image also contains a preinstalled React, TypeScript, Vite, Storybook,
accessibility-addon, and Lucide toolchain under `/opt/storybook-template`. Build it
with:

```bash
make sandbox-image
```

The image build has two stages:

1. The Node stage runs `npm ci` from the committed lockfile, type-checks the
   template, and completes a static Storybook build as a smoke test.
2. The final Python Playwright stage receives Node, npm, the immutable template,
   and its complete `node_modules` tree.

Dependency resolution therefore happens only while the trusted image is built.
Creator containers continue to run with Docker networking disabled. Their npm
configuration is forced offline, Storybook telemetry is disabled, and generated
source can use only the dependencies already present in the template. Storybook's
writable build cache is redirected from the immutable dependency tree to `/tmp`.

Each creator job receives only `project.json`. It returns only `render.json`, one
representative screenshot, and a bounded static Storybook archive. Generated
paths, imports, source sizes, component-plan references, and CSF exports are
validated before the job starts and independently checked again inside the
sandbox.

The template deliberately excludes Tailwind. It uses ordinary imported CSS and
includes `lucide-react` for local icons. Exact dependency versions live in
`sandbox/storybook-template/package.json`; transitive versions and integrity
hashes live in its lockfile. The Node and Playwright base-image defaults are also
digest-pinned. Dockerfile build arguments provide the explicit upgrade path.

Individual commands remain available:

```bash
make sync
make browsers
make analyzer-up
make analyzer-down
make test
make lint
make format
make typecheck
```
