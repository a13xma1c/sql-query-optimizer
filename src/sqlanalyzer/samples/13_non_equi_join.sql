-- name: Non-equi join
-- expect: non-equi-join
-- description: A range condition as the only join predicate forces a nested loop.
SELECT o.id, s.id AS shipment_id
FROM orders o
JOIN shipments s ON s.shipped_at > o.ts
WHERE o.id < 100
