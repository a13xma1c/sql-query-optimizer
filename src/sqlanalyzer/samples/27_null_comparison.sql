-- name: Comparison with NULL using =
-- expect: null-comparison
-- rewrites: null-comparison
-- description: customer_id = NULL is never true; IS NULL was meant.
SELECT o.id FROM orders o WHERE o.customer_id = NULL
