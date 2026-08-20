ALTER TABLE plans ADD COLUMN status TEXT NOT NULL DEFAULT '';

UPDATE plans SET status = json_extract(payload, '$.status') WHERE payload IS NOT NULL;
