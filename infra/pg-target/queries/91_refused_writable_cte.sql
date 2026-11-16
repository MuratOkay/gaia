-- Refused: write_operation.
WITH gone AS (DELETE FROM sales.orders RETURNING order_id) SELECT count(*) FROM gone
