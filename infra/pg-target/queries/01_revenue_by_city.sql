-- Accepted: a declared FK (sales.orders.store_id -> store.stores), a date function, the row cap.
SELECT s.city, date_trunc('month', o.order_date) AS month, sum(o.total_amount) AS revenue
FROM sales.orders o
JOIN store.stores s ON s.store_id = o.store_id
WHERE o.status <> 'cancelled'
GROUP BY 1, 2
ORDER BY 3 DESC
