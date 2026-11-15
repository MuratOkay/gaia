# Measurement report — demo_pg (postgres)

Started 2026-11-14T14:26:16+00:00, 231 s wall time. Tool v1.

## Environment

| Setting | Value |
|---|---|
| version | 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1) |
| database | gaia_target_pg |
| server_started_at | 2026-11-14 14:26:12.91567+00 |
| shared_buffers | 256MB |
| track_io_timing | off |
| max_parallel_workers_per_gather | 2 |
| transaction_read_only | on |
| maxdop | 2 |
| isolation | read_committed (autocommit, read-only) |
| statement_timeout_s | 300 |
| maxdop | 2 |
| sample_rows | 10,000 |
| seed | 42 |
| max_columns | 60 |

## Findings

- sales.order_events: exact tier 1+2 (count + batched scan + 6 exact distinct counts) = 28.2 s
- sales.order_events: non-key exact COUNT(DISTINCT) median 3299 ms, worst 11557 ms on payload (1016879 distinct)
- sales.order_events: pg_stats estimate error vs exact: median 0.2 %, max 52.0 %
- sales.order_events.event_id: COUNT(DISTINCT) 2189 ms vs GROUP BY form 2296 ms (6715455 distinct)
- sales.order_events.event_type: COUNT(DISTINCT) 2972 ms vs GROUP BY form 1217 ms (6 distinct)
- sales.order_events.event_id: AVG+STDEV native 1086 ms vs float cast 414 ms (first; like for like, 2.6x)
- sales.order_events.event_id: AVG+STDEV native 1913 ms vs float cast 880 ms (repeat; like for like, 2.2x)
- sales.order_events: median exact 2094 ms, sample 5 ms
- sales.order_events: system sample of 10000 rows returned 8749 rows in 3 ms, 81 pages
- sales.order_events: bernoulli sample of 10000 rows returned 9831 rows in 117 ms, 62304 pages
- finance.gl_entries: exact tier 1+2 (count + batched scan + 8 exact distinct counts) = 46.0 s
- finance.gl_entries: non-key exact COUNT(DISTINCT) median 4337 ms, worst 11561 ms on doc_ref (2150105 distinct)
- finance.gl_entries: pg_stats estimate error vs exact: median 0.0 %, max 98.6 %
- finance.gl_entries.entry_id: COUNT(DISTINCT) 978 ms vs GROUP BY form 1686 ms (6488806 distinct)
- finance.gl_entries.doc_ref: COUNT(DISTINCT) 11561 ms vs GROUP BY form 10928 ms (2150105 distinct)
- finance.gl_entries.doc_type: COUNT(DISTINCT) 4337 ms vs GROUP BY form 1590 ms (2 distinct)
- finance.gl_entries.debit: AVG+STDEV native 872 ms vs float cast 1004 ms (first; like for like, 0.9x)
- finance.gl_entries.debit: AVG+STDEV native 1172 ms vs float cast 1156 ms (repeat; like for like, 1.0x)
- finance.gl_entries.credit: AVG+STDEV native 1232 ms vs float cast 986 ms (first; like for like, 1.2x)
- finance.gl_entries.credit: AVG+STDEV native 1144 ms vs float cast 1394 ms (repeat; like for like, 0.8x)
- finance.gl_entries: median exact 4487 ms, sample 8 ms
- finance.gl_entries: system sample of 10000 rows returned 9700 rows in 6 ms, 100 pages
- finance.gl_entries: bernoulli sample of 10000 rows returned 10068 rows in 269 ms, 66904 pages
- sales.orders: exact tier 1+2 (count + batched scan + 19 exact distinct counts) = 38.7 s
- sales.orders: non-key exact COUNT(DISTINCT) median 1569 ms, worst 4681 ms on total_amount (1148554 distinct)
- sales.orders: pg_stats estimate error vs exact: median 0.0 %, max 82.3 %
- sales.orders.order_id: COUNT(DISTINCT) 344 ms vs GROUP BY form 438 ms (2500000 distinct)
- sales.orders.ordered_at: COUNT(DISTINCT) 1569 ms vs GROUP BY form 5342 ms (2444279 distinct)
- sales.orders.currency_code: COUNT(DISTINCT) 1918 ms vs GROUP BY form 273 ms (3 distinct)
- sales.orders.tax_total: AVG+STDEV native 342 ms vs float cast 455 ms (first; like for like, 0.8x)
- sales.orders.tax_total: AVG+STDEV native 481 ms vs float cast 594 ms (repeat; like for like, 0.8x)
- sales.orders.subtotal: AVG+STDEV native 403 ms vs float cast 545 ms (first; like for like, 0.7x)
- sales.orders.subtotal: AVG+STDEV native 478 ms vs float cast 624 ms (repeat; like for like, 0.8x)
- sales.orders: median exact 1914 ms, sample 9 ms
- sales.orders: system sample of 10000 rows returned 8688 rows in 4 ms, 154 pages
- sales.orders: bernoulli sample of 10000 rows returned 10035 rows in 152 ms, 44288 pages
- footprint: 0 statistics object(s) appeared during the run
- TABLESAMPLE on materialized view rpt.mv_product_margin: supported
- TABLESAMPLE on partitioned table sales.order_events: supported
- TABLESAMPLE on view rpt.v_customer_overview: NOT supported — TABLESAMPLE clause can only be applied to tables and materialized views

## sales.order_events

6,715,455 rows (estimate), 510,681,088 bytes, 6/6 columns measured.

| Operation | Column | Key | First ms | Repeat ms | Pages | Physical | Spill | Value | Note |
|---|---|---|---|---|---|---|---|---|---|
| count |  |  | 469.9 | 820.6 | 62,304 | 62,046 | 0 | 6,715,455 |  |
| scan |  |  | 1,222.1 | 2,011.7 | 62,304 | 61,662 | 0 | 6,715,455 |  |
| distinct_exact | event_id |  | 2,189.0 | 2,838.0 | 25,743 | 0 | 0 | 6,715,455 |  |
| distinct_stats | event_id |  | 3.7 |  |  |  |  | 6,715,917 |  |
| distinct_exact | order_id |  | 1,267.0 |  |  |  |  | 2,500,000 |  |
| distinct_stats | order_id |  | 3.3 |  |  |  |  | 1,200,072 |  |
| distinct_exact | event_type |  | 2,972.5 | 4,695.7 | 62,304 | 61,660 | 39,488 | 6 |  |
| distinct_stats | event_type |  | 6.0 |  |  |  |  | 6.0 |  |
| distinct_exact | event_at |  | 3,626.3 |  |  |  |  | 6,403,946 |  |
| distinct_stats | event_at |  | 2.1 |  |  |  |  | 6,428,700 |  |
| distinct_exact | actor |  | 4,926.0 |  |  |  |  | 43 |  |
| distinct_stats | actor |  | 2.5 |  |  |  |  | 43.0 |  |
| distinct_exact | payload |  | 11,557.2 |  |  |  |  | 1,016,879 |  |
| distinct_stats | payload |  | 3.5 |  |  |  |  | 947,265.0 |  |
| distinct_grouped | event_id |  | 2,296.3 |  |  |  |  | 6,715,455 |  |
| distinct_grouped | event_type |  | 1,217.3 |  |  |  |  | 6 |  |
| avg_stdev_native | event_id |  | 1,086.2 | 1,913.1 | 62,304 | 61,372 | 0 | 3,357,728 |  |
| avg_stdev_float | event_id |  | 413.6 | 880.2 | 62,304 | 60,988 | 0 | 3,357,728 |  |
| avg_float | event_id |  | 422.4 | 805.8 | 62,304 | 60,636 | 0 | 3,357,728 |  |
| percentile_exact | event_id |  | 2,094.1 | 3,193.2 | 62,304 | 60,348 | 34,730 | 3,357,728 |  |
| percentile_sample | event_id |  | 5.3 | 4.7 | 81 | 0 | 0 | 3,588,291 |  |
| sample_count_system |  |  | 2.6 | 4.1 | 81 | 0 | 0 | 8,749 |  |
| sample_count_bernoulli |  |  | 116.6 | 116.1 | 62,304 | 60,075 | 0 | 9,831 |  |
| sample_fetch |  |  | 27.2 |  |  |  |  |  | 8749 rows fetched |
| topk | event_type |  | 589.5 | 1,110.6 | 62,318 | 59,787 | 0 |  |  |

## finance.gl_entries

6,488,806 rows (estimate), 548,257,792 bytes, 8/8 columns measured.

| Operation | Column | Key | First ms | Repeat ms | Pages | Physical | Spill | Value | Note |
|---|---|---|---|---|---|---|---|---|---|
| count |  |  | 429.2 | 417.8 | 6,018 | 0 | 0 | 6,488,806 |  |
| scan |  |  | 2,404.1 | 2,727.4 | 66,904 | 66,808 | 0 | 6,488,806 |  |
| distinct_exact | entry_id | yes | 977.5 | 1,713.6 | 17,734 | 0 | 0 | 6,488,806 |  |
| distinct_stats | entry_id | yes | 1.5 |  |  |  |  | 6,488,806 |  |
| distinct_exact | posted_on |  | 3,094.4 |  |  |  |  | 1,096 |  |
| distinct_stats | posted_on |  | 2.6 |  |  |  |  | 1,096.0 |  |
| distinct_exact | account_no |  | 3,540.8 |  |  |  |  | 5 |  |
| distinct_stats | account_no |  | 6.1 |  |  |  |  | 5.0 |  |
| distinct_exact | debit |  | 7,607.1 |  |  |  |  | 1,040,189 |  |
| distinct_stats | debit |  | 10.5 |  |  |  |  | 14,252.0 |  |
| distinct_exact | credit |  | 9,686.6 |  |  |  |  | 1,138,249 |  |
| distinct_stats | credit |  | 4.4 |  |  |  |  | 45,750.0 |  |
| distinct_exact | doc_type |  | 4,336.6 | 6,353.0 | 66,904 | 66,392 | 22,310 | 2 |  |
| distinct_stats | doc_type |  | 4.7 |  |  |  |  | 2.0 |  |
| distinct_exact | doc_ref |  | 11,561.0 | 11,280.5 | 66,904 | 66,520 | 63,420 | 2,150,105 |  |
| distinct_stats | doc_ref |  | 28.1 |  |  |  |  | 1,077,537 |  |
| distinct_exact | fiscal_period |  | 2,410.8 |  |  |  |  | 36 |  |
| distinct_stats | fiscal_period |  | 3.3 |  |  |  |  | 36.0 |  |
| distinct_grouped | entry_id | yes | 1,686.0 |  |  |  |  | 6,488,806 |  |
| distinct_grouped | doc_ref |  | 10,927.7 |  |  |  |  | 2,150,105 |  |
| distinct_grouped | doc_type |  | 1,589.7 |  |  |  |  | 2 |  |
| avg_stdev_native | debit |  | 871.7 | 1,171.8 | 66,904 | 66,264 | 0 | 2,864.2 |  |
| avg_stdev_float | debit |  | 1,004.0 | 1,155.8 | 66,904 | 66,072 | 0 | 2,864.2 |  |
| avg_float | debit |  | 1,059.0 | 1,210.8 | 66,904 | 65,880 | 0 | 2,864.2 |  |
| avg_stdev_native | credit |  | 1,231.7 | 1,143.5 | 66,904 | 65,688 | 0 | 2,864.2 |  |
| avg_stdev_float | credit |  | 986.3 | 1,393.9 | 66,904 | 65,496 | 0 | 2,864.2 |  |
| avg_float | credit |  | 1,198.9 | 2,546.7 | 66,904 | 65,304 | 0 | 2,864.2 |  |
| percentile_exact | debit |  | 4,487.0 | 4,805.0 | 66,904 | 65,176 | 33,732 | 0.0 |  |
| percentile_sample | debit |  | 8.3 | 7.3 | 100 | 0 | 0 | 0.0 |  |
| sample_count_system |  |  | 6.1 | 2.3 | 100 | 0 | 0 | 9,700 |  |
| sample_count_bernoulli |  |  | 268.6 | 248.8 | 66,904 | 65,014 | 0 | 10,068 |  |
| sample_fetch |  |  | 132.7 |  |  |  |  |  | 9700 rows fetched |
| topk | doc_type |  | 2,350.1 | 1,098.8 | 66,918 | 64,886 | 0 |  |  |

## sales.orders

2,500,000 rows (estimate), 362,930,176 bytes, 19/19 columns measured.

| Operation | Column | Key | First ms | Repeat ms | Pages | Physical | Spill | Value | Note |
|---|---|---|---|---|---|---|---|---|---|
| count |  |  | 586.7 | 741.7 | 1,993 | 0 | 0 | 2,500,000 |  |
| scan |  |  | 3,492.6 | 2,364.5 | 44,288 | 44,192 | 0 | 2,500,000 |  |
| distinct_exact | order_id | yes | 344.3 | 516.5 | 6,835 | 0 | 0 | 2,500,000 |  |
| distinct_stats | order_id | yes | 1.6 |  |  |  |  | 2,500,000 |  |
| distinct_exact | order_no | yes | 802.6 |  |  |  |  | 2,500,000 |  |
| distinct_stats | order_no | yes | 3.7 |  |  |  |  | 2,500,000 |  |
| distinct_exact | customer_id |  | 1,147.7 |  |  |  |  | 249,992 |  |
| distinct_stats | customer_id |  | 5.9 |  |  |  |  | 225,987.0 |  |
| distinct_exact | store_id |  | 669.1 |  |  |  |  | 40 |  |
| distinct_stats | store_id |  | 2.1 |  |  |  |  | 40.0 |  |
| distinct_exact | channel |  | 834.7 |  |  |  |  | 4 |  |
| distinct_stats | channel |  | 3.0 |  |  |  |  | 4.0 |  |
| distinct_exact | status |  | 255.0 |  |  |  |  | 6 |  |
| distinct_stats | status |  | 3.6 |  |  |  |  | 6.0 |  |
| distinct_exact | order_date |  | 1,063.7 |  |  |  |  | 1,096 |  |
| distinct_stats | order_date |  | 3.7 |  |  |  |  | 1,096.0 |  |
| distinct_exact | ordered_at |  | 1,569.3 | 1,774.5 | 44,288 | 43,776 | 7,368 | 2,444,279 |  |
| distinct_stats | ordered_at |  | 2.1 |  |  |  |  | 2,406,936 |  |
| distinct_exact | shipped_at |  | 885.4 |  |  |  |  | 849,830 |  |
| distinct_stats | shipped_at |  | 2.4 |  |  |  |  | 836,664.0 |  |
| distinct_exact | delivered_at |  | 977.5 |  |  |  |  | 760,281 |  |
| distinct_stats | delivered_at |  | 5.0 |  |  |  |  | 763,917.0 |  |
| distinct_exact | currency_code |  | 1,917.5 | 1,553.2 | 44,288 | 43,648 | 9,835 | 3 |  |
| distinct_stats | currency_code |  | 7.6 |  |  |  |  | 3.0 |  |
| distinct_exact | subtotal |  | 4,446.4 |  |  |  |  | 1,083,097 |  |
| distinct_stats | subtotal |  | 2.7 |  |  |  |  | 191,527.0 |  |
| distinct_exact | tax_total |  | 3,751.4 |  |  |  |  | 441,187 |  |
| distinct_stats | tax_total |  | 4.0 |  |  |  |  | 85,225.0 |  |
| distinct_exact | shipping_fee |  | 3,152.7 |  |  |  |  | 3 |  |
| distinct_stats | shipping_fee |  | 4.6 |  |  |  |  | 3.0 |  |
| distinct_exact | total_amount |  | 4,681.4 |  |  |  |  | 1,148,554 |  |
| distinct_stats | total_amount |  | 3.7 |  |  |  |  | 222,606.0 |  |
| distinct_exact | payment_method |  | 2,824.0 |  |  |  |  | 4 |  |
| distinct_stats | payment_method |  | 10.1 |  |  |  |  | 4.0 |  |
| distinct_exact | coupon_code |  | 2,202.5 |  |  |  |  | 9,000 |  |
| distinct_stats | coupon_code |  | 5.1 |  |  |  |  | 8,790.0 |  |
| distinct_exact | sales_rep_id |  | 1,436.3 |  |  |  |  | 366 |  |
| distinct_stats | sales_rep_id |  | 4.9 |  |  |  |  | 366.0 |  |
| distinct_exact | source_system |  | 1,645.4 |  |  |  |  | 4 |  |
| distinct_stats | source_system |  | 1.5 |  |  |  |  | 4.0 |  |
| distinct_grouped | order_id | yes | 438.1 |  |  |  |  | 2,500,000 |  |
| distinct_grouped | ordered_at |  | 5,341.8 |  |  |  |  | 2,444,279 |  |
| distinct_grouped | currency_code |  | 272.9 |  |  |  |  | 3 |  |
| avg_stdev_native | subtotal |  | 403.0 | 477.8 | 44,288 | 43,520 | 0 | 6,952.2 |  |
| avg_stdev_float | subtotal |  | 545.1 | 623.8 | 44,288 | 43,328 | 0 | 6,952.2 |  |
| avg_float | subtotal |  | 533.0 | 681.6 | 44,288 | 43,136 | 0 | 6,952.2 |  |
| avg_stdev_native | tax_total |  | 341.9 | 480.9 | 44,288 | 42,944 | 0 | 1,154.5 |  |
| avg_stdev_float | tax_total |  | 455.2 | 593.8 | 44,288 | 42,752 | 0 | 1,154.5 |  |
| avg_float | tax_total |  | 506.7 | 568.1 | 44,288 | 42,560 | 0 | 1,154.5 |  |
| percentile_exact | subtotal |  | 1,914.4 | 2,101.5 | 44,288 | 42,432 | 5,663 | 3,992.9 |  |
| percentile_sample | subtotal |  | 8.7 | 9.1 | 154 | 0 | 0 | 3,921.3 |  |
| sample_count_system |  |  | 3.9 | 5.6 | 154 | 0 | 0 | 8,688 |  |
| sample_count_bernoulli |  |  | 152.5 | 133.4 | 44,288 | 42,225 | 0 | 10,035 |  |
| sample_fetch |  |  | 69.3 |  |  |  |  |  | 8688 rows fetched |
| topk | currency_code |  | 473.2 | 524.7 | 44,302 | 42,097 | 0 |  |  |

## TABLESAMPLE support

| Object | Kind | Supported | Rows | ms | Error |
|---|---|---|---|---|---|
| rpt.mv_product_margin | materialized view | yes | 0 | 4.4 |  |
| sales.order_events | partitioned table | yes | 59,975 | 48.1 |  |
| rpt.v_customer_overview | view | no |  | 3.2 | TABLESAMPLE clause can only be applied to tables and materialized views |

Times: SQL Server = client wall time; PostgreSQL repeat = server execution time (EXPLAIN ANALYZE). Pages are 8 KB. Physical reads on a first run prove a cold cache.