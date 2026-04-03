# Repo Release Prep Sprint

## Scope
- Prepare the Knowledge Terminal directory for a clean public GitHub repo push.
- Exclude local runtime, secrets, and private sample assets from source control.
- Add a public-facing README that explains the project, setup, and usage.

## Assumptions
- Personal PDFs, images, AgentFS databases, and local virtualenv contents should not be pushed.
- Existing source code, tests, PRD, and sprint notes are acceptable to keep in the repo.

## Architectural Decisions
- Repository hygiene is handled through `.gitignore`, not code changes.
- Public onboarding is handled through `README.md` and `.env` stays local-only.

## Tasks
1. Tighten `.gitignore` for Python caches, runtime state, local databases, and private sample assets.
2. Add a root `README.md` with overview, architecture, setup, and usage.
3. Run verification to ensure the repo still passes tests after documentation and ignore updates.

## Risks
- Overly broad ignore rules could hide files future contributors may want to commit.
- README drift is possible if setup commands do not match the current codebase.

## Verification
- Review the final `.gitignore` patterns against local files in the repo root.
- Run the full test suite.
