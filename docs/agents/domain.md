# Domain Docs

The engineering skills use a single-context domain documentation layout for this repo.

## Before exploring

- Read `CONTEXT.md` at the repo root when it exists.
- Read relevant decisions in `docs/adr/` when they exist.

Proceed when either is absent. The `/domain-modeling` skill creates these files when terms or decisions are resolved.

## File structure

```text
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

## Use the glossary's vocabulary

Use terms defined in `CONTEXT.md` when naming domain concepts in issues, proposals, hypotheses, and tests. If a needed term is absent, reconsider the name or note the gap for `/domain-modeling`.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, identify the ADR and explain why the decision should be revisited.
