-- name: LEFT JOIN turned inner by WHERE
-- expect: left-join-filtered
-- rewrites: left-join-to-inner
-- description: The WHERE predicate on the right table drops the NULL-padded rows, so the join is effectively inner.
SELECT o.id, c.name
FROM orders o
LEFT JOIN customers c ON c.id = o.customer_id
WHERE c.region = 'EU'
