"""CAIOS deliverables — Phase 6 (CAIOS-DEL-001).

Document/workbook generation as a governed pipeline: a structured spec is
stored with the deliverable row, a `report.generate` job renders the bytes
through the format builders, storage keeps the artefact and the row keeps
only keys/metadata. Events trail every transition.
"""
