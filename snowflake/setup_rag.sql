-- 1) Choose context (adjust names as needed)
use role ACCOUNTADMIN;
create warehouse if not exists CHI_POLLUTION_WH warehouse_size = 'XSMALL' auto_suspend = 60 auto_resume = true;

create database if not exists CHI_POLLUTION_DB;
create schema if not exists CHI_POLLUTION_DB.RAG;

use warehouse CHI_POLLUTION_WH;
use database CHI_POLLUTION_DB;
use schema RAG;

-- 2) RAG source table
create or replace table RAG_DOCS (
  doc_id string,
  chunk_id string,
  content string,
  source_url string
);

-- 3) Example seed data (replace with your real pollution docs/chunks)
insert into RAG_DOCS (doc_id, chunk_id, content, source_url) values
  ('baseline-report', '1', 'Downtown Chicago typically shows higher NO2 during 5pm-7pm due to congestion.', 'https://example.org/chicago-report-1'),
  ('baseline-report', '2', 'Heavy trucks emit disproportionately high PM2.5 compared with passenger vehicles.', 'https://example.org/chicago-report-1'),
  ('baseline-report', '3', 'Lakefront corridors generally have lower pollution due to wind dispersion effects.', 'https://example.org/chicago-report-2');

-- 4) Cortex Search index
create or replace cortex search service CHI_POLLUTION_SEARCH
on content
attributes doc_id, chunk_id, source_url
warehouse = CHI_POLLUTION_WH
target_lag = '1 minute'
as (
  select doc_id, chunk_id, content, source_url
  from RAG_DOCS
);

-- 5) Quick smoke test for retrieval
select snowflake.cortex.search_preview(
  'CHI_POLLUTION_DB.RAG.CHI_POLLUTION_SEARCH',
  '{"query":"Where are peak emissions in Chicago?", "columns":["content","source_url","doc_id","chunk_id"], "limit":3}'
);
