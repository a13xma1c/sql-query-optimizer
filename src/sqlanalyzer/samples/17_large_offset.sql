-- name: OFFSET pagination deep into the result
-- expect: large-offset
-- description: The engine produces and discards the first 50 000 rows on every page.
SELECT o.id, o.ts, o.amount
FROM orders o
ORDER BY o.ts, o.id
LIMIT 50 OFFSET 50000
