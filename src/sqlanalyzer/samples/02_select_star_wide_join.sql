-- name: SELECT * over a wide join
-- expect: select-star, unfiltered-scan
-- description: Every column of both tables is read although only two are needed downstream.
SELECT *
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
