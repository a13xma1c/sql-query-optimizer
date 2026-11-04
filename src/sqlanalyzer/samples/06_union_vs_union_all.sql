-- name: UNION where UNION ALL would do
-- expect: union-distinct
-- rewrites: union-all
-- description: The two branches cannot overlap (disjoint statuses), so the de-duplication is wasted work.
SELECT o.id, o.amount FROM orders o WHERE o.status = 'a'
UNION
SELECT o.id, o.amount FROM orders o WHERE o.status = 'b'
