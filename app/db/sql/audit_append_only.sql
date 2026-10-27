-- Run after the tables exist (apply via an Alembic migration).
-- audit_logs is append-only: block UPDATE and DELETE.
CREATE OR REPLACE FUNCTION audit_logs_block_changes() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_logs is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs;
CREATE TRIGGER trg_audit_logs_append_only
  BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION audit_logs_block_changes();

-- evaluation_decisions are never edited either: a new row supersedes the old one.
DROP TRIGGER IF EXISTS trg_decisions_no_update ON evaluation_decisions;
CREATE TRIGGER trg_decisions_no_update
  BEFORE UPDATE OR DELETE ON evaluation_decisions
  FOR EACH ROW EXECUTE FUNCTION audit_logs_block_changes();

-- postings are a record of what was sent to the academic system: never edited or deleted.
DROP TRIGGER IF EXISTS trg_postings_no_update ON postings;
CREATE TRIGGER trg_postings_no_update
  BEFORE UPDATE OR DELETE ON postings
  FOR EACH ROW EXECUTE FUNCTION audit_logs_block_changes();
