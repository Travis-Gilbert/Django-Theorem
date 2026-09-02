-- SPEC-THEOREM-CONTROL-PLANE-1.0 D1 / A1
-- Roles enforce schema ownership: control vs spine.
-- Apply as a superuser against the shared Postgres instance.

BEGIN;

-- Schemas
CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS spine;

-- Roles (idempotent-ish: ignore if exist)
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'theorem_control') THEN
    CREATE ROLE theorem_control LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'theorem_spine') THEN
    CREATE ROLE theorem_spine LOGIN;
  END IF;
END
$$;

-- Ownership
ALTER SCHEMA control OWNER TO theorem_control;
ALTER SCHEMA spine OWNER TO theorem_spine;

-- theorem_control owns control; no privileges on spine
REVOKE ALL ON SCHEMA spine FROM theorem_control;
REVOKE ALL ON ALL TABLES IN SCHEMA spine FROM theorem_control;
GRANT USAGE, CREATE ON SCHEMA control TO theorem_control;
GRANT ALL ON ALL TABLES IN SCHEMA control TO theorem_control;
GRANT ALL ON ALL SEQUENCES IN SCHEMA control TO theorem_control;
ALTER DEFAULT PRIVILEGES FOR ROLE theorem_control IN SCHEMA control
  GRANT ALL ON TABLES TO theorem_control;
ALTER DEFAULT PRIVILEGES FOR ROLE theorem_control IN SCHEMA control
  GRANT ALL ON SEQUENCES TO theorem_control;

-- theorem_spine owns spine; SELECT only on the five D3 control tables
GRANT USAGE ON SCHEMA spine TO theorem_spine;
GRANT ALL ON ALL TABLES IN SCHEMA spine TO theorem_spine;
GRANT ALL ON ALL SEQUENCES IN SCHEMA spine TO theorem_spine;

GRANT USAGE ON SCHEMA control TO theorem_spine;
-- Exact D3 read-model surface (tables created by Django migrations):
GRANT SELECT (
  id, slug, display_name, is_active
) ON control.control_tenant TO theorem_spine;

GRANT SELECT (
  tenant_id, slug, display_name
) ON control.control_project TO theorem_spine;

GRANT SELECT (
  tenant_id, plan_code, status
) ON control.control_subscription TO theorem_spine;

GRANT SELECT (
  code, limits
) ON control.control_plan TO theorem_spine;

GRANT SELECT (
  id, tenant_id, key_hash, scopes, revoked_at, expires_at
) ON control.control_apikey TO theorem_spine;

GRANT SELECT (
  id, tenant_id, operation, contract_version, source_kind, source_ref, params,
  params_hash, status, shard_count, rows_total, error, created_at, updated_at
) ON control.control_extractionjob TO theorem_spine;

GRANT SELECT (
  id, tenant_id, job_id, candidate_digest, candidate_digest_version, claim_id, decision,
  merge_target_claim_id, reason, reviewer, created_at
) ON control.control_extractionreview TO theorem_spine;

-- Explicitly no write on control for spine
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA control FROM theorem_spine;

-- theorem_control must not touch spine
REVOKE USAGE ON SCHEMA spine FROM theorem_control;

COMMIT;

-- Notes:
-- 1. Run Django migrations as theorem_control with search_path=control,public.
-- 2. Column-level GRANTs require the tables to exist; re-run the GRANT block
--    after the first Django migrate, or wrap in a post-migrate hook.
-- 3. Password / connection strings are provisioned out of band (Railway).
