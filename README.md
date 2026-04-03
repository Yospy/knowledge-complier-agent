# Knowledge Terminal

```text
------------------------------------------------------------
|                                                          |
|   ██╗  ██╗███╗   ██╗ ██████╗ ██╗    ██╗██╗     ███████╗   |
|   ██║ ██╔╝████╗  ██║██╔═══██╗██║    ██║██║     ██╔════╝   |
|   █████╔╝ ██╔██╗ ██║██║   ██║██║ █╗ ██║██║     █████╗     |
|   ██╔═██╗ ██║╚██╗██║██║   ██║██║███╗██║██║     ██╔══╝     |
|   ██║  ██╗██║ ╚████║╚██████╔╝╚███╔███╔╝███████╗███████╗   |
|   ╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝  ╚══╝╚══╝ ╚══════╝╚══════╝   |
|                                                          |
|              Terminal-native knowledge system            |
|                                                          |
------------------------------------------------------------
```

Knowledge Terminal is an agentic knowledge system that ingests PDFs, images, markdown, and text into a persistent virtual filesystem, compiles them into structured markdown knowledge files, and lets users query that knowledge through a read-only terminal chat interface.

## What It Does

- Ingests documents through a FastAPI backend.
- Stores raw sources and compiled knowledge in AgentFS.
- Uses the OpenAI Agents SDK to:
  - extract concepts from sources
  - write `/raw`, `/wiki/concepts`, and `/wiki/index.md`
  - answer terminal queries with a read-only query agent
- Exposes a terminal-native chat client that shows tool calls and streamed answers.

## Architecture

- `src/akc/api/app.py`
  FastAPI app with:
  - `POST /ingest` for ingestion
  - `POST /chat` for read-only streaming terminal queries

- `src/akc/agents/ingestion.py`
  Ingestion agent that reads the source, writes raw markdown, creates concept files, and updates the knowledge index.

- `src/akc/agents/query.py`
  Read-only query agent that explores AgentFS using shell-like tools:
  - `ls`
  - `cd`
  - `cat`
  - `grep`

- `src/akc/storage/agentfs_store.py`
  AgentFS-backed persistence layer for:
  - `/raw`
  - `/wiki/concepts`
  - `/wiki/index.md`
  - `/logs`
  - duplicate-ingest hash registry

- `a.py`
  Rich-based terminal client for live chat against the backend on `:8000`.

## Repository Layout

```text
.
├── a.py
├── src/akc/
│   ├── agents/
│   ├── api/
│   ├── storage/
│   ├── config.py
│   └── main.py
├── tests/
├── sprints/
└── agentic_knowledge_compiler_prd.md
```

## Setup

### 1. Create and activate a virtualenv

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -e .
```

### 3. Create `.env`

```bash
OPENAI_API_KEY=your_key_here
AKC_MODEL=gpt-5.1
```

Optional:

```bash
AKC_AGENTFS_DB_PATH=.agentfs/akc.db
```

## Running the Backend

```bash
source .venv/bin/activate
PYTHONPATH=src uvicorn akc.main:app --host 127.0.0.1 --port 8000
```

## Ingesting Sources

### Markdown

```bash
curl -X POST http://127.0.0.1:8000/ingest \
  -F "files=@/absolute/path/to/file.md;type=text/markdown"
```

### PDF

```bash
curl -X POST http://127.0.0.1:8000/ingest \
  -F "files=@/absolute/path/to/file.pdf;type=application/pdf"
```

### Inline text

```bash
curl -X POST http://127.0.0.1:8000/ingest \
  -F "text=Your source text here"
```

## Running Terminal CLI Chat

Start the backend first, then run:

```bash
source .venv/bin/activate
python a.py
```

The terminal client:

- connects to `http://127.0.0.1:8000/chat`
- keeps chat history locally in memory
- shows tool calls while answering
- queries the same AgentFS knowledge base created during ingestion

Local terminal commands:

- `/help`
- `/clear`
- `/exit`
- `/quit`

## Testing

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## Notes

- `.env`, `.venv`, `.agentfs`, and private local sample assets are intentionally ignored.
- The terminal query agent is read-only by design and cannot modify the knowledge base.
- AgentFS is used as the system of record for both ingestion-time compilation and query-time retrieval.

# knowledge-complier-agent
