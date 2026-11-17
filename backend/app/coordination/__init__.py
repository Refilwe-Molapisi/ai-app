"""Central coordination package — Phase 4 (CAIOS-MASTER-001 §11, §12, §13).

The assignment/workflow engine: declarative workflow registry, assignments
with ordered steps (automatic queue-executed + human gates), an append-only
governance event trail, and API routes. Steps execute as ordinary 'assignment.step'
queue jobs inside the runner budget; the shared AI service powers knowledge
steps so the §20 ledger covers workflow model calls too.
"""
