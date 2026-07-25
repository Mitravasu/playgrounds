# Playgrounds

Analyze a website's rendered design system and generate matching UI components
inside isolated browser sandboxes.

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

Build the local browser image once. Docker reuses its cached image layers for
each disposable job:

```bash
docker build --tag playgrounds-browser:latest sandbox
```

With the analyzer proxy running, analyze the trusted POC page:

```bash
docker compose -f docker-compose.analyzer-egress.yml up --build --detach
uv run playgrounds analyze https://www.mitravasu.com/
```

The command writes validated evidence and a synthesized `style-guide.json` to
`runs/<run-id>/analysis/`.

Individual commands remain available:

```bash
make sync
make browsers
make test
make lint
make format
make typecheck
```
