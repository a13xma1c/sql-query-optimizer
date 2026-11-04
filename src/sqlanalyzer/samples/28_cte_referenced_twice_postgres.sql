-- name: CTE referenced twice (PostgreSQL materialises it)
-- engines: postgres
-- expect: cte-materialized
-- demo: no
-- description: Outer predicates are not pushed into a CTE PostgreSQL materialises; NOT MATERIALIZED inlines it.
WITH big AS (
    SELECT o.id, o.customer_id, o.amount FROM orders o
)
SELECT a.id, b.id
FROM big a
JOIN big b ON b.customer_id = a.customer_id AND b.id > a.id
WHERE a.amount > 490
