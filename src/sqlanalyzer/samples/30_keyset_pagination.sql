-- name: Keyset pagination (the fix for OFFSET)
-- expect-none: true
-- description: Continues after the last seen key instead of skipping rows.
SELECT o.id, o.ts, o.amount
FROM orders o
WHERE o.ts > TIMESTAMP '2024-06-01 00:00:00' OR (o.ts = TIMESTAMP '2024-06-01 00:00:00' AND o.id > 1000)
ORDER BY o.ts, o.id
LIMIT 50
