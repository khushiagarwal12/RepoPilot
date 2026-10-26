# Interview notes (all claims map to code and tests in this repo)

**What is it?** Repository Q&A: ingest -> parse/chunk -> embed -> retrieve -> grounded answer with verified citations.

**PostgreSQL and ChromaDB can't share a transaction. How do you stay consistent?** Each ingestion job builds its own Chroma collection *before* the DB commit; the committed `succeeded` job is the pointer search reads. Failure rolls back the DB and drops the half-built collection, leaving the previous data and index intact. Orphans from crashes are never queried. Tests: `test_vector_pipeline.py`.

**Security decisions?** Repository code is never executed (AST only). ZIPs: validate every member first, skip symlinks, stage the extraction, count real bytes, clean up on failure. Downloads: exact host allow-list, manual redirects restricted to GitHub hosts (SSRF), streamed size/time caps. Logs redact secrets. Tests: `test_security_archive.py`, `test_github_download.py`.

**How do you stop hallucinated answers?** Evidence gate (no LLM call when the best score is low), citations only count if they point to real retrieved chunks and are rebuilt from DB rows, uncited answers are rejected. Repository text is untrusted: delimited with a random token, the model has no tools. Tests: `test_ask.py` (with mutation checks).

**Why not LangGraph / pgvector?** The flows are linear, and one vector store keeps things simple (`docs/DECISIONS.md`).

**A bug the tests had hidden:** the old symlink test asserted the buggy behaviour. I fixed the code, flipped the test, and mutation-tested that the new tests fail if the bug returns.

**Honest limits:** hybrid retrieval and the score threshold need evaluation on the real model; Docker/CI are unexecuted; no auth.
