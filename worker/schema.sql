CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  labels TEXT NOT NULL,
  last_t INTEGER,
  last_v REAL,
  UNIQUE (name, labels)
);
CREATE TABLE IF NOT EXISTS points (
  series_id INTEGER NOT NULL,
  t INTEGER NOT NULL,
  v REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS points_by_series ON points (series_id, t);
CREATE TABLE IF NOT EXISTS targets (
  url TEXT PRIMARY KEY,
  labels TEXT
);
