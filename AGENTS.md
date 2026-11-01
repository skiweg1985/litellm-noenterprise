## Documentation

Write short, factual documentation in English. Keep `README.md` current
with the project's purpose, implementation status, and next steps.

## Git workflow

- Start each change on a branch based on the latest `origin/main`.
- Use `feature/<short-description>` for new functionality and
  `fix/<short-description>` for bug fixes. Use `docs/<short-description>`
  for documentation and `chore/<short-description>` for maintenance.
- Push the branch and open a pull request targeting `main`.
- Run the checks relevant to the change and resolve review discussions
  before merging through the pull request.
- `main` is protected, including for administrators. Keep its protection
  enabled; direct pushes, force pushes, and deletion are prohibited.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues. Before working with tickets,
read `docs/agents/issue-tracker.md`.

### Triage labels

Use the five canonical triage labels. Before triaging issues,
read `docs/agents/triage-labels.md`.

### Domain docs

This repo uses a single-context layout. Before exploring the codebase,
read `docs/agents/domain.md`.
