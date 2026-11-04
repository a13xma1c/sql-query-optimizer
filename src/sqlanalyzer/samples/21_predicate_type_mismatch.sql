-- name: Literal of the wrong type
-- expect: predicate-type-mismatch
-- schema: yes
-- description: amount is numeric but compared with a string; the column gets cast instead of the literal.
SELECT o.id FROM orders o WHERE o.amount = '499'
