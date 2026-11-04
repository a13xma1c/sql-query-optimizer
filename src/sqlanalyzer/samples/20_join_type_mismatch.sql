-- name: Join on columns of different types
-- expect: join-type-mismatch
-- schema: yes
-- description: shipments.order_ref is VARCHAR while orders.id is INTEGER: one side is cast on every row.
SELECT o.id, s.carrier
FROM orders o
JOIN shipments s ON s.order_ref = o.id
WHERE o.status = 'a'
