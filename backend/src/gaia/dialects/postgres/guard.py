"""PostgreSQL SQL guard (Phase 4A, D52: the main dialect).

The rules common to all dialects are in ``gaia.query.validator``. What is
PostgreSQL's here: the parser dialect and the **function allowlist**.

An allowlist, not a denylist, because PostgreSQL has functions that undo the
session's protections from inside a ``SELECT`` in a read-only transaction:
``set_config('statement_timeout', '0', false)`` lifts the timeout,
``query_to_xml('…')`` runs SQL text, ``dblink`` reaches other servers,
``pg_advisory_lock`` takes locks, ``pg_sleep`` holds a connection,
``pg_read_file`` / ``lo_import`` read the server's files, ``nextval`` writes.
None of them is on the list, and neither is any ``pg_*``, ``txid_*`` or
``current_setting`` function. Names are checked as the target will see them
(the rendered call), whatever class the parser gave the node.

Explicitly out of scope of the list (add deliberately, with a test): XML and
full-text functions, ``format`` (it is safe, but never needed for answers),
anything returning server or session information.
"""

from __future__ import annotations

from gaia.query.validator import SqlGuardBase

__all__ = ["POSTGRES_FUNCTIONS", "PostgresGuard"]

POSTGRES_FUNCTIONS = frozenset({
    # aggregates
    "count", "sum", "avg", "min", "max", "stddev", "stddev_pop", "stddev_samp", "variance",
    "var_pop", "var_samp", "string_agg", "array_agg", "bool_and", "bool_or", "every",
    "percentile_cont", "percentile_disc", "mode", "corr", "covar_pop", "covar_samp",
    "regr_slope", "regr_intercept", "regr_r2", "json_agg", "jsonb_agg", "json_object_agg",
    "jsonb_object_agg", "grouping",
    # window
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
    "first_value", "last_value", "nth_value",
    # conditional and comparison
    "coalesce", "nullif", "greatest", "least", "num_nulls", "num_nonnulls",
    # math
    "abs", "ceil", "ceiling", "floor", "round", "trunc", "sign", "sqrt", "cbrt", "power",
    "pow", "exp", "ln", "log", "log10", "mod", "div", "pi", "width_bucket", "scale",
    "random",
    # text
    "lower", "upper", "initcap", "length", "char_length", "character_length", "octet_length",
    "substr", "substring", "position", "strpos", "trim", "btrim", "ltrim", "rtrim", "lpad",
    "rpad", "replace", "translate", "concat", "concat_ws", "left", "right", "split_part",
    "reverse", "repeat", "starts_with", "ascii", "chr", "md5", "regexp_replace",
    "regexp_match", "regexp_matches", "regexp_like", "regexp_count", "regexp_substr",
    "regexp_split_to_array", "regexp_split_to_table", "to_char", "to_number", "to_date",
    "to_timestamp", "unaccent",
    # date and time
    "now", "current_date", "current_timestamp", "localtimestamp", "date_trunc", "date_part",
    "date_bin", "age", "make_date", "make_time", "make_timestamp", "make_timestamptz",
    "make_interval", "justify_days", "justify_hours", "justify_interval", "isfinite",
    "clock_timestamp", "statement_timestamp", "transaction_timestamp", "timezone",
    # json and arrays
    "json_build_object", "jsonb_build_object", "json_build_array", "jsonb_build_array",
    "to_json", "to_jsonb", "json_extract_path", "jsonb_extract_path",
    "json_extract_path_text", "jsonb_extract_path_text", "json_array_length",
    "jsonb_array_length", "json_typeof", "jsonb_typeof", "array_length", "array_position",
    "array_positions", "array_to_string", "string_to_array", "array_remove", "array_cat",
    "array_append", "array_prepend", "cardinality", "unnest",
    # set-returning (also in FROM)
    "generate_series",
})


class PostgresGuard(SqlGuardBase):
    dialect = "postgres"
    read = "postgres"
    functions = POSTGRES_FUNCTIONS
    denied_function_classes = frozenset({"NextValueFor"})
