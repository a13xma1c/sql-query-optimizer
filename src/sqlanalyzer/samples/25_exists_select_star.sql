-- name: EXISTS with SELECT *
-- rewrites: exists-select-1
-- description: The projected columns of an EXISTS subquery are never evaluated.
SELECT c.id
FROM customers c
WHERE EXISTS (SELECT * FROM orders o WHERE o.customer_id = c.id AND o.amount > 400)
