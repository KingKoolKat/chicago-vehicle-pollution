-- Snowflake RAG chunking setup for schema:
--   RAG_DOCUMENTS(DOC_ID, DOC_CONTENT, CATEGORY, FILE_NAME)
--
-- Run in your target DB/SCHEMA (adjust USE statements as needed).

use database MY_MODAL_DB;
use schema PUBLIC;

-- 1) Chunk table
create table if not exists RAG_DOCUMENT_CHUNKS (
  chunk_id number autoincrement start 1 increment 1,
  doc_id varchar,
  category varchar,
  file_name varchar,
  chunk_text string
);

-- Optional: clear and rebuild
truncate table RAG_DOCUMENT_CHUNKS;

-- 2A) Preferred chunking path (Cortex splitter), if enabled in your account
-- NOTE: If this fails in your account, use 2B fallback below.
insert into RAG_DOCUMENT_CHUNKS (doc_id, category, file_name, chunk_text)
select
  d.doc_id,
  d.category,
  d.file_name,
  f.value::string as chunk_text
from RAG_DOCUMENTS d,
     lateral flatten(
       input => snowflake.cortex.split_text_recursive_character(
         d.doc_content,
         'none',
         1200,  -- chunk size
         150    -- overlap
       )
     ) f
where d.doc_content is not null
  and trim(d.doc_content) <> '';

-- 2B) Fallback chunking path (pure SQL, no Cortex function)
-- Uncomment this block only if 2A is unavailable in your account.
/*
insert into RAG_DOCUMENT_CHUNKS (doc_id, category, file_name, chunk_text)
with nums as (
  select seq4() as n
  from table(generator(rowcount => 2000))
),
doc_chunks as (
  select
    d.doc_id,
    d.category,
    d.file_name,
    (n * 1050) + 1 as start_pos
  from RAG_DOCUMENTS d
  join nums on (n * 1050) < length(d.doc_content)
  where d.doc_content is not null
    and trim(d.doc_content) <> ''
)
select
  doc_id,
  category,
  file_name,
  substr(d.doc_content, c.start_pos, 1200) as chunk_text
from doc_chunks c
join RAG_DOCUMENTS d using (doc_id)
where trim(substr(d.doc_content, c.start_pos, 1200)) <> '';
*/

-- 3) Cortex Search service on chunk_text
create or replace cortex search service UNIVERSAL_SEARCH_SERVICE
on chunk_text
attributes doc_id, category, file_name, chunk_id
warehouse = COMPUTE_WH
target_lag = '1 minute'
as (
  select doc_id, category, file_name, chunk_id, chunk_text
  from RAG_DOCUMENT_CHUNKS
);

-- 4) Smoke tests
select count(*) as chunk_count from RAG_DOCUMENT_CHUNKS;

select snowflake.cortex.search_preview(
  'MY_MODAL_DB.PUBLIC.UNIVERSAL_SEARCH_SERVICE',
  parse_json('{
    "query": "recommendations to reduce vehicle emissions",
    "columns": ["chunk_text","doc_id","category","file_name","chunk_id"],
    "limit": 20
  }')
);
