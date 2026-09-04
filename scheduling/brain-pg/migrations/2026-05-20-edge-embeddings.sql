-- 2026-05-20: edge relation embeddings
--
-- Why: Practical GraphRAG paper (arxiv 2507.03226) shows hybrid retrieval
-- combining graph structure with semantic similarity benefits from
-- maintaining SEPARATE embeddings for entities, chunks, and relations.
-- Currently we have entity + evidence embeddings; edges only have type +
-- confidence. Adding edge embeddings lets the retrieval layer find
-- "what edges connect to my query semantically" — useful for queries about
-- relationships, not just entities or evidence.
--
-- Backfill plan: embed.py-style helper script embeds the text
-- "<from_name> <edge_type> <to_name>" for each of ~7594 edges. ~$0.03 in
-- Voyage credits (voyage-3-large, ~150K tokens).
--
-- The column is nullable so this migration is non-blocking — edges remain
-- functional without embedding; the new fourth leg in query.py just
-- ignores edges with NULL embedding.

ALTER TABLE entity_edges ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- HNSW index for cosine similarity, matching the entities + evidence pattern.
CREATE INDEX IF NOT EXISTS idx_entity_edges_embedding_hnsw
  ON entity_edges
  USING hnsw (embedding vector_cosine_ops);

-- ─── Reverse SQL ─────────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS idx_entity_edges_embedding_hnsw;
-- ALTER TABLE entity_edges DROP COLUMN IF EXISTS embedding;
