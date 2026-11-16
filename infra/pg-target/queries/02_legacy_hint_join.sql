-- Accepted on a host where the model proposed legacy.tb_trx02.f2 -> crm.customers.customer_code
-- and its claim test passed (D48): join kind "hint". Rejected (unverified_join) where it did not.
SELECT c.segment, count(*) AS transactions, sum(t.f7) AS amount
FROM legacy.tb_trx02 t
JOIN crm.customers c ON c.customer_code = t.f2
GROUP BY c.segment
ORDER BY amount DESC
