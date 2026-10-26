# Decisions
- PostgreSQL = source of truth for text, lines and jobs; ChromaDB = vectors only (no pgvector/Pinecone).
- One Chroma collection per job + "latest succeeded job" pointer, because the two stores cannot share a transaction.
- `ast` for Python parsing; Tree-sitter only when other languages are added.
- Symlinks in ZIPs are skipped (GitHub archives contain them), everything else unsafe is rejected.
- Explicit database selection: no silent SQLite fallback (`REPOPILOT_ALLOW_SQLITE=1` opt-in for dev/tests).
- Chroma never reads `.env` (`_env_file=None`); client created lazily.
- No LangGraph: the pipelines are linear. Revisit only for a real multi-step agent loop.
- Retrieval: chunk-overlap dedupe is always on (the chunker emits overlapping windows). Hybrid (vector + keyword, reciprocal-rank fusion) is opt-in via `RETRIEVAL_MODE=hybrid`; it is NOT the default until `scratch/eval_retrieval.py` shows a gain with the real embedding model. LangGraph is not used: retrieval and ask are linear.
- Rate limiting is an in-memory per-IP limiter (`RATE_LIMIT_PER_MINUTE`, 0 disables): enough for one process, not a substitute for a proxy limiter.
