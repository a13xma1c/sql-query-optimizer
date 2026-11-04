-- name: The same table scanned three times
-- expect: repeated-scan
-- description: Three scans of orders for three aggregates; conditional aggregation scans once.
SELECT
    (SELECT COUNT(*) FROM orders o WHERE o.status = 'a') AS a_count,
    (SELECT COUNT(*) FROM orders o WHERE o.status = 'b') AS b_count,
    (SELECT COUNT(*) FROM orders o WHERE o.status = 'c') AS c_count
