-- P1: immutable content-addressed mappings and profiles.
CREATE TABLE IF NOT EXISTS connection_documents (
    document_ref TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_documents_kind_digest
    ON connection_documents (kind, digest);
