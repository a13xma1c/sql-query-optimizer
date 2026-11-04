-- name: OR on the same column (IN list candidate)
-- rewrites: or-to-in
-- description: Repeated equalities on one column are cheaper as an IN list.
SELECT o.id
FROM orders o
WHERE o.status = 'a' OR o.status = 'b' OR o.status = 'c'
