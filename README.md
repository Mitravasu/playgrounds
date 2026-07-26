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

Build the local browser image and recreate the analyzer proxy, then analyze a
public HTTPS page:

```bash
make analyzer-up
uv run playgrounds analyze https://www.mitravasu.com/
```

The command writes validated evidence and a synthesized `style-guide.json` to
`runs/<run-id>/analysis/`.

Generate a component manually from that run:

```bash
uv run playgrounds create run_<id> "Create a dropdown with three account actions."
```

The same commands are available through Make:

```bash
make analyzer URL=https://www.mitravasu.com/
make creator RUN=run_<id> PROMPT="Create a dropdown with three account actions."
```

The creator sends the source screenshot and style guide to the model, renders
the generated HTML/CSS/JavaScript in an offline sandbox, records browser
diagnostics, and scores the result with a fresh reviewer context. It makes at
most one revision. The selected files are written to
`components/<creation-id>/` and the full attempt history remains under the run.

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
