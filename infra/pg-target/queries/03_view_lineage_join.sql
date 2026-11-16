-- Accepted with 02: the view's customer_code is tb_trx02.f2 (view lineage), so the
-- same hint backs this join (join kind "hint_via_view").
SELECT c.segment, sum(v.amount) AS amount
FROM rpt.v_legacy_transactions v
JOIN crm.customers c ON c.customer_code = v.customer_code
GROUP BY c.segment
