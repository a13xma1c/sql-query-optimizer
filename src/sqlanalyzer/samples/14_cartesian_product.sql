-- name: Accidental Cartesian product
-- expect: cartesian-join
-- rewrites: explicit-join
-- description: Two tables listed in FROM with no join predicate between them.
SELECT o.id, c.name
FROM orders o, customers c
WHERE o.amount > 450
