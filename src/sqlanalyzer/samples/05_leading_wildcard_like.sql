-- name: LIKE with a leading wildcard
-- expect: leading-wildcard
-- description: '%smith' cannot use an index or min/max pruning.
SELECT c.id, c.name
FROM customers c
WHERE c.name LIKE '%customer 9%'
