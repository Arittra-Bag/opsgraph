-- Purpose-made, non-sensitive acceptance data; provision ONLY in a new dedicated
-- opsgraph_acceptance* database. No IF NOT EXISTS, cleanup, roles, or credentials:
-- a collision must abort the transaction rather than reuse or replace anything.
BEGIN;

CREATE SCHEMA acceptance_ledger;
CREATE TABLE acceptance_ledger.accounts (
    account_id integer PRIMARY KEY,
    account_key text NOT NULL UNIQUE,
    currency text NOT NULL
);
CREATE TABLE acceptance_ledger.invoices (
    invoice_id integer PRIMARY KEY,
    account_id integer NOT NULL REFERENCES acceptance_ledger.accounts,
    invoice_day date NOT NULL
);
CREATE TABLE acceptance_ledger.settlements (
    settlement_id integer PRIMARY KEY,
    invoice_id integer NOT NULL REFERENCES acceptance_ledger.invoices,
    signed_amount numeric(12, 2) NOT NULL,
    settlement_state text NOT NULL
);
INSERT INTO acceptance_ledger.accounts VALUES
    (1, 'A-01', 'USD'), (2, 'A-02', 'EUR'), (3, 'A-03', 'USD'),
    (4, 'A-04', 'INR'), (5, 'A-05', 'GBP'), (6, 'A-06', 'USD');
INSERT INTO acceptance_ledger.invoices VALUES
    (101, 1, '2026-09-01'), (102, 1, '2026-09-02'), (103, 1, '2026-09-03'),
    (201, 2, '2026-09-01'), (202, 2, '2026-09-02'),
    (301, 3, '2026-09-04'), (302, 3, '2026-09-05'),
    (401, 4, '2026-09-06'), (402, 4, '2026-09-07'),
    (501, 5, '2026-09-08'), (502, 5, '2026-09-09'), (503, 5, '2026-09-10');
INSERT INTO acceptance_ledger.settlements VALUES
    (1, 101, 60.00, 'posted'), (2, 101, 40.00, 'posted'),
    (3, 102, -10.00, 'posted'), (4, 102, 999.00, 'pending'),
    (5, 103, 88.00, 'declined'),
    (6, 201, 45.25, 'posted'), (7, 202, 54.75, 'posted'),
    (8, 202, 500.00, 'pending'),
    (9, 301, 250.00, 'declined'), (10, 302, 250.00, 'pending'),
    (11, 401, 1234.56, 'posted'), (12, 402, 765.44, 'posted'),
    (13, 402, 100.00, 'declined'),
    (14, 501, 40.00, 'posted'), (15, 502, 10.00, 'posted'),
    (16, 503, 50.00, 'pending'), (17, 503, 50.00, 'declined'),
    (18, 503, 25.00, 'cancelled');

CREATE SCHEMA acceptance_dispatch;
CREATE TABLE acceptance_dispatch.routes (
    route_id integer PRIMARY KEY,
    route_code text NOT NULL UNIQUE
);
CREATE TABLE acceptance_dispatch.parcels (
    parcel_id integer PRIMARY KEY,
    route_id integer NOT NULL REFERENCES acceptance_dispatch.routes
);
CREATE TABLE acceptance_dispatch.scans (
    scan_id integer PRIMARY KEY,
    parcel_id integer NOT NULL REFERENCES acceptance_dispatch.parcels,
    scan_kind text NOT NULL,
    observed_at timestamptz NOT NULL
);
INSERT INTO acceptance_dispatch.routes VALUES
    (1, 'R-A'), (2, 'R-B'), (3, 'R-C'), (4, 'R-D'), (5, 'R-E');
INSERT INTO acceptance_dispatch.parcels VALUES
    (1, 1), (2, 1), (3, 1), (4, 1), (5, 2), (6, 2),
    (7, 2), (8, 3), (9, 3), (10, 3), (11, 4), (12, 4);
INSERT INTO acceptance_dispatch.scans VALUES
    (1, 1, 'pickup', '2026-09-11T10:00:00Z'),
    (2, 1, 'dropoff', '2026-09-11T11:00:00Z'),
    (3, 1, 'dropoff', '2026-09-11T11:00:01Z'),
    (4, 2, 'pickup', '2026-09-11T10:05:00Z'),
    (5, 4, 'pickup', '2026-09-11T10:10:00Z'),
    (6, 4, 'dropoff', '2026-09-11T11:10:00Z'),
    (7, 5, 'pickup', '2026-09-11T10:15:00Z'),
    (8, 6, 'pickup', '2026-09-11T10:20:00Z'),
    (9, 6, 'dropoff', '2026-09-11T11:20:00Z'),
    (10, 9, 'pickup', '2026-09-11T10:25:00Z'),
    (11, 10, 'dropoff', '2026-09-11T11:25:00Z'),
    (12, 11, 'pickup', '2026-09-11T10:30:00Z'),
    (13, 11, 'dropoff', '2026-09-11T11:30:00Z');

CREATE SCHEMA acceptance_metering;
CREATE TABLE acceptance_metering.probes (
    probe_id integer PRIMARY KEY,
    probe_key text NOT NULL UNIQUE
);
CREATE TABLE acceptance_metering.readings (
    reading_id integer PRIMARY KEY,
    probe_id integer NOT NULL REFERENCES acceptance_metering.probes,
    reading_value numeric(14, 4),
    recorded_local timestamp without time zone NOT NULL,
    reading_state text NOT NULL
);
INSERT INTO acceptance_metering.probes VALUES (1, 'P-A'), (2, 'P-B'), (3, 'P-C');
INSERT INTO acceptance_metering.readings VALUES
    (1, 1, 12.3400, '2026-09-10T23:55:00', 'q'),
    (2, 1, 13.1000, '2026-09-11T00:05:00', 'r'),
    (3, 1, NULL, '2026-09-11T12:00:00', 'x'),
    (4, 2, 0.0010, '2026-09-11T00:10:00', 'q'),
    (5, 2, 0.0020, '2026-09-11T12:10:00', 'r'),
    (6, 2, 0.0000, '2026-09-11T23:50:00', 'q'),
    (7, 3, 999.9900, '2026-09-11T23:55:00', 'q'),
    (8, 3, NULL, '2026-09-12T00:05:00', 'x'),
    (9, 3, 1001.2300, '2026-09-12T12:00:00', 'r');

CREATE SCHEMA acceptance_drift;
CREATE TABLE acceptance_drift.records (record_id integer PRIMARY KEY, marker text NOT NULL);
INSERT INTO acceptance_drift.records VALUES (1, 'one'), (2, 'two'), (3, 'three');

COMMIT;
