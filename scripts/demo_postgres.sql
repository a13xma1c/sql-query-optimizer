CREATE TABLE IF NOT EXISTS customers (id int PRIMARY KEY, name text, region text, signup_date date, email text);
CREATE TABLE IF NOT EXISTS products (id int PRIMARY KEY, sku varchar(16) UNIQUE, category text, price numeric(10, 2));
CREATE TABLE IF NOT EXISTS orders (id int PRIMARY KEY, customer_id int, ts timestamp, status text, amount int);
CREATE TABLE IF NOT EXISTS order_items (order_id int, product_id int, quantity int, price numeric(10, 2));
CREATE TABLE IF NOT EXISTS refunds (order_id int, amt int, reason text);
CREATE TABLE IF NOT EXISTS shipments (id int PRIMARY KEY, order_ref varchar(16), shipped_at timestamp, carrier text);
CREATE TABLE IF NOT EXISTS events (id bigint, dt date, user_id int, kind text) PARTITION BY RANGE (dt);
CREATE TABLE IF NOT EXISTS events_01 PARTITION OF events FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
CREATE TABLE IF NOT EXISTS events_02 PARTITION OF events FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');
CREATE TABLE IF NOT EXISTS events_03 PARTITION OF events FOR VALUES FROM ('2024-03-01') TO ('2024-04-01');
CREATE TABLE IF NOT EXISTS events_04 PARTITION OF events FOR VALUES FROM ('2024-04-01') TO ('2024-05-01');
CREATE TABLE IF NOT EXISTS events_05 PARTITION OF events FOR VALUES FROM ('2024-05-01') TO ('2024-06-01');
CREATE TABLE IF NOT EXISTS events_06 PARTITION OF events FOR VALUES FROM ('2024-06-01') TO ('2024-07-01');
CREATE TABLE IF NOT EXISTS events_07 PARTITION OF events FOR VALUES FROM ('2024-07-01') TO ('2024-08-01');
CREATE TABLE IF NOT EXISTS events_08 PARTITION OF events FOR VALUES FROM ('2024-08-01') TO ('2024-09-01');
CREATE TABLE IF NOT EXISTS events_09 PARTITION OF events FOR VALUES FROM ('2024-09-01') TO ('2024-10-01');
CREATE TABLE IF NOT EXISTS events_10 PARTITION OF events FOR VALUES FROM ('2024-10-01') TO ('2024-11-01');
CREATE TABLE IF NOT EXISTS events_11 PARTITION OF events FOR VALUES FROM ('2024-11-01') TO ('2024-12-01');
CREATE TABLE IF NOT EXISTS events_12 PARTITION OF events FOR VALUES FROM ('2024-12-01') TO ('2025-01-01');

INSERT INTO customers
    SELECT i, CASE WHEN i % 50 = 0 THEN NULL ELSE 'customer ' || i END, (ARRAY['EU', 'US', 'APAC'])[1 + i % 3],
           DATE '2020-01-01' + (i % 1500), 'user' || i || '@example.com'
    FROM generate_series(1, 1000) i;
INSERT INTO products
    SELECT i, 'SKU-' || lpad(i::text, 5, '0'),
           (ARRAY['books', 'toys', 'garden', 'tools', 'food', 'music', 'games', 'home'])[1 + i % 8], 5 + (i % 200) * 1.5
    FROM generate_series(1, 500) i;
INSERT INTO orders
    SELECT i, 1 + i % 1000, TIMESTAMP '2024-01-01' + (i % 365) * INTERVAL '1 day' + (i % 24) * INTERVAL '1 hour',
           (ARRAY['a', 'b', 'c', 'd'])[1 + i % 4], i % 500
    FROM generate_series(1, 200000) i;
INSERT INTO order_items
    SELECT 1 + i % 200000, 1 + i % 500, 1 + i % 5, 5 + (i % 200) * 1.5 FROM generate_series(1, 400000) i;
INSERT INTO refunds SELECT i * 37, i % 9, (ARRAY['damaged', 'late', 'other'])[1 + i % 3] FROM generate_series(1, 5000) i;
INSERT INTO shipments
    SELECT i, (1 + i % 200000)::text, TIMESTAMP '2024-01-02' + (i % 365) * INTERVAL '1 day',
           (ARRAY['dhl', 'ups', 'fedex', 'post'])[1 + i % 4]
    FROM generate_series(1, 150000) i;
INSERT INTO events
    SELECT i, DATE '2024-01-01' + (i % 366), i % 5000, (ARRAY['view', 'click', 'buy'])[1 + i % 3]
    FROM generate_series(1, 120000) i;
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_id);
CREATE INDEX IF NOT EXISTS orders_ts_idx ON orders (ts);
CREATE INDEX IF NOT EXISTS order_items_order_idx ON order_items (order_id);
ANALYZE;
