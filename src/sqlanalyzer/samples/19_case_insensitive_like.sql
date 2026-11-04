-- name: LOWER(column) LIKE
-- engines: postgres, duckdb, snowflake
-- expect: case-insensitive-like
-- rewrites: ilike
-- description: Wrapping the column in LOWER() defeats indexes; ILIKE keeps the column bare.
SELECT c.id, c.email
FROM customers c
WHERE LOWER(c.email) LIKE 'user1%'
