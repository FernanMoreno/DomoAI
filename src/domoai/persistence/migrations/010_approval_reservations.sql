CREATE TABLE IF NOT EXISTS approval_reservations (
    approval_id TEXT PRIMARY KEY REFERENCES approval_grants(approval_id),
    reservation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'committed', 'released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (reservation_id, approval_id)
);

CREATE INDEX IF NOT EXISTS approval_reservations_by_reservation
    ON approval_reservations (reservation_id, status);
