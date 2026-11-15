-- Meridian platform database schema.
-- The tables platform-core reads and writes through its db access layer.

CREATE TABLE accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    limit_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    id         TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts (id),
    total      REAL NOT NULL
);

CREATE TABLE transactions (
    txn        TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts (id),
    amount     REAL NOT NULL,
    currency   TEXT NOT NULL DEFAULT 'USD'
);

-- Append-only ledger: every money movement, written by meridian.ledger.record.
CREATE TABLE ledger_entries (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts (id),
    amount     REAL NOT NULL,
    txn        TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'sync',
    reversed   INTEGER NOT NULL DEFAULT 0
);

-- The audit trail, written by meridian.audit.write.
CREATE TABLE audit_log (
    id     INTEGER PRIMARY KEY,
    event  TEXT NOT NULL,
    detail TEXT NOT NULL
);
