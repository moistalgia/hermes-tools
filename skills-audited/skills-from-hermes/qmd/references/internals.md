# QMD — How the Search Pipeline Works

Understanding the internals helps choose the right search mode.

1. **Query Expansion** — A fine-tuned 1.7B model generates 2 alternative
   queries. The original gets 2x weight in fusion.
2. **Parallel Retrieval** — BM25 (SQLite FTS5) and vector search run
   simultaneously across all query variants.
3. **RRF Fusion** — Reciprocal Rank Fusion (k=60) merges results. Top-rank
   bonus: #1 gets +0.05, #2-3 get +0.02.
4. **LLM Reranking** — qwen3-reranker scores top 30 candidates (0.0-1.0).
5. **Position-Aware Blending** — Ranks 1-3: 75% retrieval / 25% reranker.
   Ranks 4-10: 60/40. Ranks 11+: 40/60 (trusts reranker more for long tail).

**Smart Chunking:** Documents are split at natural break points (headings,
code blocks, blank lines) targeting ~900 tokens with 15% overlap. Code blocks
are never split mid-block.
