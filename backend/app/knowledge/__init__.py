"""Knowledge & documents package — Phase 2 (CAIOS-MASTER-001 §7, §9).

Ingestion pipeline: upload (storage) → knowledge.extract (text extraction)
→ knowledge.embed (chunk + embed + pgvector upsert) → /knowledge/search.
"""
