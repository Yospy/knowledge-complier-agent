# Agentic Knowledge Compiler (AKC) — Product Requirements Document

## 1. Overview

The Agentic Knowledge Compiler (AKC) is a system that ingests raw knowledge artifacts (PDF, Markdown, Text) and transforms them into a structured, queryable knowledge base using an LLM-powered agent. The system leverages a virtual filesystem (AgentFS) as its persistent memory and uses the OpenAI Agents SDK for orchestration.

The system operates in two primary modes:

1. Ingestion Mode (API-driven)
2. Query Mode (CLI-driven)

---

## 2. Goals

- Convert unstructured data into structured knowledge
- Maintain a continuously evolving markdown-based knowledge base
- Enable efficient querying via concept-level reasoning
- Avoid reliance on external databases or vector stores
- Use filesystem as the primary interface for storage and retrieval

---

## 3. Non-Goals

- No vector database in V1
- No embeddings-based retrieval in V1
- No UI beyond CLI
- No manual editing workflows
- No multi-agent orchestration in V1

---

## 4. System Architecture

### Components

- OpenAI Agents SDK (orchestrator)
- GPT-5.x model (reasoning engine)
- AgentFS (virtual filesystem)
- FastAPI (ingest endpoint)
- CLI interface (query engine)

### Data Flow

Ingestion:
Input → /ingest → Agent → AgentFS (/raw, /wiki) → index + concepts

Query:
CLI → Agent → index.md → concept files → synthesis → output

---

## 5. Filesystem Design (AgentFS)

```
/
├── raw/
│   ├── <source_id>.md
│
├── wiki/
│   ├── index.md
│   ├── concepts/
│   │   ├── <concept_name>.md
│
├── logs/
│   ├── ingestion.log
```

---

## 6. Ingestion API

### Endpoint

POST /ingest

### Supported Inputs

- PDF
- Markdown
- Text

### Processing Steps

1. Convert input to markdown
2. Save to /raw/<source_id>.md
3. Trigger agent workflow:
   - Extract concepts
   - Create/update concept files
   - Update index.md
4. On failure, log error to /logs/ingestion.log

---

## 7. Agent Responsibilities

### Core Responsibilities

- Parse raw content
- Identify concepts and entities
- Create or update concept files
- Maintain relationships between concepts
- Update index.md

### Tooling Interface

The agent will have access to the following tools:

- list_dir(path)
- read_file(path)
- write_file(path, content)
- append_file(path, content)
- search_files(query, path)
- run_shell(command)

---

## 8. Knowledge Representation

### Concept Files

Each concept is stored as a markdown file.

#### Template

```
# <Concept Name>

## Summary
<concise explanation>

## Key Ideas
- point 1
- point 2

## Source
- File: /raw/<source_id>.md
- Extracted At: <timestamp>

## Related Concepts
- [[concept_a]]
- [[concept_b]]
```

### Rules

- Avoid duplication
- Merge intelligently on updates
- Maintain clear, concise structure

---

## 9. Index Design (index.md)

### Purpose

- Serve as navigation layer
- Help agent discover relevant files

### Structure

```
# Knowledge Index

## Concepts
- [[Concept A]]
- [[Concept B]]

## Sources
- /raw/source_1.md
- /raw/source_2.md

## Recently Updated
- [[Concept X]]
```

### Constraints

- No summaries
- No long-form text
- Strictly references only

---

## 10. Query Interface (CLI)

### Usage

```
> python cli.py
> what are risks in agentic coding?
```

### Query Execution Flow

1. Read index.md
2. Identify relevant concepts
3. Perform text search if needed
4. Read selected concept files
5. Synthesize answer
6. Return response

### Constraints

- Do not persist query outputs

---

## 11. Search Strategy

### V1 Implementation

- Plain text search
- grep via shell tool
- filename matching

### Example

```
grep -r "agentic coding" /wiki/concepts/
```

---

## 12. Error Handling

### Ingestion Failure

- Log error in /logs/ingestion.log

#### Format

```
[timestamp] ERROR: <description>
File: <source_id>
```

---

## 13. Autonomy Model

The agent operates in fully autonomous mode with the following expectations:

- Can create, update, and modify files
- Must adhere to markdown templates
- Must not introduce noise into index.md
- Must include source attribution and timestamps

---

## 14. Design Principles

1. Filesystem as database
2. Precompute knowledge at ingestion
3. Keep index lightweight
4. Favor modular concept files
5. Enable iterative improvement via agent

---

## 15. Future Extensions

- Embedding-based retrieval
- Multi-agent workflows
- Domain-specific indexes
- Versioning and diff tracking
- Validation and consistency checks

---

## 16. Success Criteria

- Accurate concept extraction
- Clean and navigable index
- Fast query response
- Minimal redundancy
- Stable ingestion pipeline

---

## 17. Risks

- Concept drift
- Redundant concept creation
- Index pollution
- Agent over-modification

Mitigation strategies will be introduced in future versions.

---

## 18. Summary

The Agentic Knowledge Compiler transforms raw information into structured, navigable knowledge using an LLM agent operating over a virtual filesystem. The system emphasizes simplicity, autonomy, and leveraging markdown as a universal interface for both humans and machines.

