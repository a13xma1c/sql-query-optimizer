-- name: DISTINCT on top of GROUP BY
-- rewrites: redundant-distinct
-- description: GROUP BY already makes the rows unique.
SELECT DISTINCT o.status, COUNT(*) AS n
FROM orders o
GROUP BY o.status
