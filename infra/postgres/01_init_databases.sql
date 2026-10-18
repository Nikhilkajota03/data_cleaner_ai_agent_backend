-- ==============================================================================
-- 01_init_databases.sql
-- Initializes the two logically isolated databases:
-- 1. governance_db: System audit trails, state checkpoints, pre-images
-- 2. staging_db: Target enterprise business database containing CRM data
-- ==============================================================================

CREATE DATABASE governance_db;
CREATE DATABASE staging_db;
