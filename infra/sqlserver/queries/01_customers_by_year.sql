-- Accepted if dbo.transactions.customer_id has a declared FK to dbo.customers (join kind "fk");
-- rejected as unverified_join if it has none. Either way, send the output.
SELECT TOP 10 YEAR(t.occurred_at) AS y, COUNT_BIG(DISTINCT c.customer_id) AS customers,
       SUM(t.total) AS total
FROM dbo.transactions t
JOIN dbo.customers c ON c.customer_id = t.customer_id
GROUP BY YEAR(t.occurred_at)
ORDER BY y
