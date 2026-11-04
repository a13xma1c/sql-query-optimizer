-- name: Correlated scalar subquery in the select list
-- expect: correlated-subquery
-- description: The subquery runs once per customer; a pre-aggregated join or window function does it once.
SELECT c.id, c.name,
       (SELECT MAX(o.amount) FROM orders o WHERE o.customer_id = c.id) AS max_amount
FROM customers c
WHERE c.region = 'EU'
