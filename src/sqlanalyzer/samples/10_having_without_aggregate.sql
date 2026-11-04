-- name: HAVING used as a row filter
-- expect: having-without-aggregate
-- rewrites: having-to-where
-- description: The status predicate only references a GROUP BY column and belongs in WHERE.
SELECT o.status, COUNT(*) AS n, SUM(o.amount) AS total
FROM orders o
GROUP BY o.status
HAVING o.status <> 'd' AND COUNT(*) > 10
