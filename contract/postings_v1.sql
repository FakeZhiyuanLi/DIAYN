-- postings.db, contract v1: the whole schema DIAYN's db_init() creates.
--
-- The bot may read only the tables and columns CONTRACT.md lists. The rest is
-- here so that a fixture built from this file has the live file's shape:
-- `etags` and the rows of `llm_cache` are the scraper's own bookkeeping, and
-- the bot reads nothing from them but COUNT(*) of llm_cache.
--
-- tests/test_contract.py builds one database from this file and one with
-- db_init(), and requires them to match table by table, column by column and
-- index by index. Change both or neither, and read CONTRACT.md's versioning
-- rules first.
--
-- postings has no INTEGER PRIMARY KEY, so its rowids are SQLite's own. They
-- are part of the contract (P3): never VACUUM this file, rebuild it, or
-- replace a row of postings.

PRAGMA journal_mode = WAL;

-- Permanent dedup ledger. Never pruned (P2). first_seen is epoch seconds,
-- written once, and equal to the same row's postings.first_seen (P1).
CREATE TABLE IF NOT EXISTS seen(
  platform TEXT, external_id TEXT, first_seen REAL,
  PRIMARY KEY(platform, external_id));

-- Prunable detail table: only rows inside the retention window
-- (scraper_meta.prune_days, never below 30). published and first_seen are
-- epoch seconds; published may be NULL, and unbounded=1 marks Workday's
-- "30+ days ago" bucket, which is a floor rather than a date.
CREATE TABLE IF NOT EXISTS postings(
  platform TEXT, external_id TEXT, company TEXT, sector TEXT, title TEXT,
  location TEXT, url TEXT, category TEXT, term TEXT, region TEXT,
  is_intern INT, is_tech INT, published REAL, unbounded INT,
  first_seen REAL, PRIMARY KEY(platform, external_id));
CREATE INDEX IF NOT EXISTS idx_pub ON postings(published);

-- Gemini verdicts, keyed by a hash of title and location. The bot may read
-- COUNT(*) and nothing else.
CREATE TABLE IF NOT EXISTS llm_cache(
  hash TEXT PRIMARY KEY, payload TEXT, created REAL);

-- Gemini calls per quota day. day is the local date in scraper_meta.llm_day_tz
-- (P7). The token columns were added by ALTER on older files, with the same
-- type and default as here.
CREATE TABLE IF NOT EXISTS llm_usage(
  day TEXT PRIMARY KEY, n INT, prompt_tokens INT DEFAULT 0,
  output_tokens INT DEFAULT 0);

-- The scraper's conditional-request cache. Not part of the contract.
CREATE TABLE IF NOT EXISTS etags(
  platform TEXT, slug TEXT, etag TEXT, PRIMARY KEY(platform, slug));

-- One row per committed sweep. started is epoch seconds; the bot reads
-- started, duration, errors and new_rows.
CREATE TABLE IF NOT EXISTS sweeps(
  started REAL, duration REAL, not_modified INT, errors INT,
  new_rows INT, pruned INT);

-- The contract tables, refreshed at start-up and inside every sweep's
-- transaction (P8). Values in scraper_meta are text; CONTRACT.md lists the
-- keys.
CREATE TABLE IF NOT EXISTS scraper_meta(key TEXT PRIMARY KEY, value TEXT);

-- The board registry the scraper polls, after the blocklist.
CREATE TABLE IF NOT EXISTS boards(
  platform TEXT, slug TEXT, company TEXT, sector TEXT,
  PRIMARY KEY(platform, slug));

-- The blocklist as written, one name per row. The bot applies it as a
-- normalised prefix (B7); contract/company_norm_cases.json pins the rule.
CREATE TABLE IF NOT EXISTS blocked_companies(name TEXT PRIMARY KEY);

PRAGMA user_version = 2;
