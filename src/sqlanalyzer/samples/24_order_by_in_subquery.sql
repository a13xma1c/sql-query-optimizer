-- name: ORDER BY inside a derived table
-- expect: order-by-in-subquery
-- rewrites: subquery-order-by
-- description: Sorting inside the subquery does not order the outer result; it is wasted work.
SELECT x.id, x.amount
FROM (SELECT o.id, o.amount FROM orders o WHERE o.status = 'b' ORDER BY o.amount DESC) x
WHERE x.amount > 100
