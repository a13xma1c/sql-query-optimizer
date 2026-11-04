-- name: Comma join with a non-sargable date filter and NOT IN
-- expect: implicit-join, non-sargable-predicate, not-in-subquery, select-star
-- rewrites: explicit-join, sargable-date, not-in-to-not-exists
-- description: The classic "it worked on 1000 rows" query: implicit join, a function on the filter column, NOT IN.
SELECT *
FROM orders o, customers c
WHERE o.customer_id = c.id
  AND CAST(o.ts AS DATE) = DATE '2024-01-15'
  AND o.id NOT IN (SELECT r.order_id FROM refunds r)
