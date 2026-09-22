# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a body file for multi-line text.
- **Read an issue**: `gh issue view <number> --comments`; also fetch labels when needed.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments` with appropriate label and state filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- **Close an issue**: `gh issue close <number> --comment "..."`

Run `gh` inside this clone so it infers `Mitravasu/playgrounds` from the remote.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set this to `yes` if external PRs should enter the triage queue.

When set to `yes`, use the corresponding `gh pr` commands. Read a PR with `gh pr view <number> --comments` and `gh pr diff <number>`. To list external PRs, use `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` and keep authors with `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` association.

GitHub shares one number space across issues and PRs. For an ambiguous `#<number>`, try `gh pr view <number>`, then `gh issue view <number>`.

## Skill instructions

- To **publish to the issue tracker**, create a GitHub issue.
- To **fetch the relevant ticket**, run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder`. The map is one issue with child issues as tickets.

- **Map**: create an issue labelled `wayfinder:map` with Notes, Decisions-so-far, and Fog sections.
- **Child ticket**: link an issue as a GitHub sub-issue. Where sub-issues are unavailable, add it to a task list in the map and put `Part of #<map>` at the top of its body. Label it `wayfinder:<type>` (`research`, `prototype`, `grilling`, or `task`).
- **Blocking**: use GitHub issue dependencies. The `blocked_by` endpoint needs the blocker's numeric database ID, available from `gh api repos/<owner>/<repo>/issues/<n> --jq .id`. Where dependencies are unavailable, put `Blocked by: #<n>, #<n>` at the top of the child body.
- **Frontier**: inspect the map's open children in map order. Skip assigned children and any child with an open blocker. The first remaining child wins.
- **Claim**: run `gh issue edit <n> --add-assignee @me` before work.
- **Resolve**: comment with the answer, close the child, and add a short decision with a link to the map's Decisions-so-far section.
