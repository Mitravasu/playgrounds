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

Individual commands remain available:

```bash
make sync
make browsers
make test
make lint
make format
make typecheck
```
