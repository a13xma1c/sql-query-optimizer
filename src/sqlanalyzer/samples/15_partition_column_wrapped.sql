-- name: Partition column wrapped in a function
-- expect: partition-column-wrapped
-- rewrites: simplify
-- description: date_trunc on the partition column disables partition pruning; the rewrite turns it into a range.
SELECT e.kind, COUNT(*) AS n
FROM events e
WHERE DATE_TRUNC('month', e.dt) = DATE '2024-03-01'
GROUP BY e.kind
