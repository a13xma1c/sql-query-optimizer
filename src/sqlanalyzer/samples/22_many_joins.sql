-- name: Many joins in one block
-- expect: many-joins
-- description: Eight relations in one query block push the join-order search past what planners do well.
SELECT o.id
FROM orders o
JOIN customers c ON c.id = o.customer_id
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
JOIN shipments s ON s.id = o.id
JOIN refunds r ON r.order_id = o.id
JOIN events e ON e.user_id = c.id
JOIN customers c2 ON c2.id = c.id
WHERE o.status = 'a'
