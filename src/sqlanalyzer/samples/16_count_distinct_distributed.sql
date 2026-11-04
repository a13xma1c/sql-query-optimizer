-- name: Exact COUNT(DISTINCT) on a distributed engine
-- engines: trino, snowflake
-- expect: count-distinct-exact, global-sort-without-limit
-- rewrites: approx-count-distinct
-- demo: no
-- description: Exact distinct counts shuffle every value; APPROX_DISTINCT streams in constant memory.
SELECT o.status, COUNT(DISTINCT o.customer_id) AS customers
FROM orders o
JOIN customers c ON c.id = o.customer_id
GROUP BY o.status
ORDER BY customers DESC
