-- name: LIMIT without ORDER BY
-- expect: limit-without-order
-- description: An arbitrary subset of rows is returned.
SELECT o.id, o.amount FROM orders o WHERE o.status = 'c' LIMIT 10
