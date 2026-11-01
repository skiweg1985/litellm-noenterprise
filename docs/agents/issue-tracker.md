# Issue tracker: GitHub

Issues and specs live in `skiweg1985/litellm-noenterprise`.
Use the `gh` CLI from this repository.

- Publish a ticket: `gh issue create --title "..." --body-file <file>`.
- Read a ticket: `gh issue view <number> --comments`.
- Read its labels: `gh issue view <number> --json labels`.
- List tickets: `gh issue list --state open`.
- Apply labels: `gh issue edit <number> --add-label "<label>"`.
- Remove labels: `gh issue edit <number> --remove-label "<label>"`.
- Comment: `gh issue comment <number> --body-file <file>`.
- Close: `gh issue close <number>`.

For multiline content, write the exact text to a temporary file and
pass it with `--body-file`.

Use native sub-issues and dependencies when available. Otherwise,
link children with `Part of #<parent>` and blockers with
`Blocked by: #<number>`. A ticket is unblocked when all blockers close.

## Pull requests as a triage surface

**PRs as a request surface: no.**
