-- name: Unused CTE
-- rewrites: unused-cte
-- description: The 'totals' CTE is never referenced; some engines still plan or materialise it.
WITH totals AS (
    SELECT o.customer_id, SUM(o.amount) AS total FROM orders o GROUP BY o.customer_id
),
recent AS (
    SELECT o.id, o.customer_id FROM orders o WHERE o.ts >= TIMESTAMP '2024-12-01 00:00:00'
)
SELECT r.id, c.name
FROM recent r
JOIN customers c ON c.id = r.customer_id
