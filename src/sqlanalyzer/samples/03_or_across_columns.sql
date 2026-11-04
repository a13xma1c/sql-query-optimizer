-- name: OR across different columns
-- expect: or-across-columns
-- description: An OR over two columns prevents index use; UNION ALL of two filtered queries usually wins.
SELECT o.id, o.amount
FROM orders o
WHERE o.status = 'a' OR o.amount = 499
