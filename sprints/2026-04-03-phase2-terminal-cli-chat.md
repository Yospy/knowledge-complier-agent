# Phase 2 Terminal CLI Chat Sprint

## Scope
- Add a read-only query/chat path to the existing FastAPI backend.
- Add a root-level `a.py` terminal client that talks to the backend on port `8000`.
- Keep the query agent restricted to shell-like read tools over AgentFS.

## Assumptions
- The backend continues to reuse the same AgentFS database already used by ingestion.
- Query sessions are client-side only; the backend is stateless between `/chat` requests.
- Streaming over HTTP NDJSON is sufficient for the v1 terminal UX.

## Architectural Decisions
- Reuse the OpenAI Agents SDK and current model configuration for the read-only query agent.
- Expose `ls`, `cd`, `cat`, and `grep` as the query tool surface.
- Keep all query activity read-only; no writes to `/raw`, `/wiki`, `/logs`, or `/system`.

## Tasks
1. Add query agent context, prompt, and read-only tool wrappers over AgentFS.
2. Add a streaming `POST /chat` route to the existing FastAPI app.
3. Add the `a.py` terminal client with Rich-based spinner and tool trace rendering.
4. Add tests for `/chat`, read-only query behavior, and CLI helper logic.
5. Fix CLI answer streaming so token fragments are coalesced into readable chunks and `assistant_final` remains the canonical answer.

## Risks
- SDK streaming event shape may differ from assumptions.
- Rich is not currently installed in the virtualenv and must not break backend imports.
- Rich live status can corrupt inline token rendering if we print raw deltas while the spinner is active.

## Verification
- Run unit and integration tests.
- Verify `/chat` streams NDJSON events.
- Verify no query path mutates AgentFS.
- Verify the terminal client renders streamed answers in readable chunks instead of punctuation soup.
