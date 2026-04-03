# Phase 1 Ingestion Sprint

## Scope

- Build a FastAPI `POST /ingest` endpoint for PDF, markdown, text, and image sources.
- Initialize a single local AgentFS store at `.agentfs/akc.db`.
- Create one OpenAI ingestion agent with constrained tools to update `/wiki/index.md` and `/wiki/concepts/*.md`.
- Return per-source ingest results with partial-success semantics.

## Assumptions

- `OPENAI_API_KEY` is present in `.env`.
- `gpt-5.4` is the default model unless overridden by environment.
- Query CLI work is deferred to phase two.
- Tests must avoid live network calls.

## Architectural Decisions

- AgentFS is the only persistent store.
- The backend writes `/raw/<source_id>.md` before the agent compiles knowledge.
- The ingestion agent is created once at startup and reused across requests.
- The model may inspect the original PDF or image input, but writes only through validated AgentFS tools.

## Tasks

1. Create the Python project manifest, source layout, and app bootstrap.
2. Implement config loading and AgentFS lifecycle management.
3. Implement source normalization and raw markdown persistence.
4. Implement constrained ingestion tools, prompt rules, and the OpenAI agent runner.
5. Implement the synchronous `/ingest` API flow and response models.
6. Add tests for storage, validation, and API behavior with mocked agent execution.
7. Run verification and review the resulting diff.

## Risks

- OpenAI SDK multimodal request shape may drift; keep the input builder isolated.
- AgentFS path validation must be strict to avoid prompt drift.
- PDF/image source handling must preserve a markdown provenance file even when the model sees the original asset.

## Verification Strategy

- Run unit and integration tests through `python -m unittest discover -s tests -v`.
- Run a quick import smoke test for the package entrypoint.
- Review the diff for path-safety, raw/wiki separation, and prompt constraints.
