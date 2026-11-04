-- name: Well-formed analytical query
-- expect-none: true
-- description: Explicit joins, sargable range, explicit columns: the baseline to compare the others against.
SELECT c.region, p.category, SUM(oi.quantity * oi.price) AS revenue
FROM orders o
JOIN customers c ON c.id = o.customer_id
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
WHERE o.ts >= TIMESTAMP '2024-03-01 00:00:00' AND o.ts < TIMESTAMP '2024-04-01 00:00:00'
GROUP BY c.region, p.category
ORDER BY revenue DESC
LIMIT 20
