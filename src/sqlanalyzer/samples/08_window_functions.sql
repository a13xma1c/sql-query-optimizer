-- name: Window function ranking (well formed)
-- expect-none: true
-- description: A clean top-N-per-group query; nothing to fix, useful to see the plan and cost model.
SELECT ranked.customer_id, ranked.id, ranked.amount
FROM (
    SELECT o.customer_id, o.id, o.amount,
           ROW_NUMBER() OVER (PARTITION BY o.customer_id ORDER BY o.amount DESC) AS rn
    FROM orders o
    WHERE o.ts >= TIMESTAMP '2024-06-01 00:00:00'
) ranked
WHERE ranked.rn <= 3
