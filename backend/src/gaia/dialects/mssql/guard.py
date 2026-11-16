"""SQL Server (T-SQL) SQL guard (Phase 4A; after PostgreSQL, D52).

The rules common to all dialects are in ``gaia.query.validator``; PostgreSQL's
are in ``dialects/postgres/guard.py``. T-SQL adds:

* the function **allowlist** (no ``xp_*`` / ``sp_*``, no ``OPENROWSET``,
  ``OPENQUERY``, ``OPENDATASOURCE``, ``OPENJSON``, no ``NEXT VALUE FOR``, no
  permission or login functions);
* object names: a three-part name must name the target database; four-part
  names (linked servers) and temporary tables (``#t``) are rejected;
* table hints: only ``NOLOCK`` / ``READUNCOMMITTED`` (the session reads
  uncommitted anyway); ``UPDLOCK``, ``XLOCK``, ``TABLOCKX``, ``HOLDLOCK`` … take locks;
* no query ``OPTION (…)`` clauses;
* the row cap is ``TOP (n)`` — or the ``FETCH`` of an ``OFFSET … FETCH`` — and a
  set operation is wrapped as ``SELECT TOP (n) * FROM (…) AS q``.

Offline tests of this guard are blocking (D52 refinement b); live SQL Server
runs are asynchronous.
"""

from __future__ import annotations

from sqlglot import exp

from gaia.query.validator import SqlGuardBase, Violation

__all__ = ["MSSQL_FUNCTIONS", "MssqlGuard"]

MSSQL_FUNCTIONS = frozenset({
    # aggregates
    "count", "count_big", "sum", "avg", "min", "max", "stdev", "stdevp", "var", "varp",
    "string_agg", "grouping", "grouping_id", "approx_count_distinct", "checksum_agg",
    # window
    "row_number", "rank", "dense_rank", "ntile", "lag", "lead", "first_value", "last_value",
    "percent_rank", "cume_dist", "percentile_cont", "percentile_disc",
    # conditional
    "isnull", "coalesce", "nullif", "iif", "choose", "greatest", "least",
    # math
    "abs", "ceiling", "floor", "round", "sign", "sqrt", "square", "power", "exp", "log",
    "log10", "pi", "rand",
    # text
    "len", "datalength", "lower", "upper", "ltrim", "rtrim", "trim", "substring", "left",
    "right", "replace", "replicate", "reverse", "charindex", "patindex", "concat",
    "concat_ws", "format", "str", "stuff", "space", "char", "nchar", "ascii", "unicode",
    "translate", "soundex", "difference", "hashbytes", "checksum", "binary_checksum",
    "string_split",
    # conversion
    "convert", "try_convert", "parse", "try_parse",
    # date and time
    "getdate", "getutcdate", "sysdatetime", "sysutcdatetime", "sysdatetimeoffset",
    "dateadd", "datediff", "datediff_big", "datepart", "datename", "datefromparts",
    "datetimefromparts", "datetime2fromparts", "timefromparts", "eomonth", "year", "month",
    "day", "isdate", "isnumeric", "switchoffset", "todatetimeoffset", "date_bucket",
    "datetrunc",
    # json (values only; OPENJSON is a rowset function and not listed)
    "json_value", "json_query", "isjson", "json_object", "json_array",
    "newid",
})

_READ_HINTS = frozenset({"NOLOCK", "READUNCOMMITTED"})


class MssqlGuard(SqlGuardBase):
    dialect = "mssql"
    read = "tsql"
    functions = MSSQL_FUNCTIONS
    denied_function_classes = frozenset({"NextValueFor", "OpenJSON"})

    def dialect_checks(self, tree: exp.Expression) -> list[Violation]:
        out: list[Violation] = []
        if tree.args.get("options"):
            out.append(Violation("query_option", "query hints (OPTION …) are not allowed"))
        for table in tree.find_all(exp.Table):
            text = table.sql(dialect=self.read)
            if isinstance(table.this, exp.Dot):
                out.append(Violation("linked_server",
                                     f"{text}: four-part names (linked servers) are not allowed"))
            elif "#" in text.split(" AS ")[0]:
                out.append(Violation("temp_table", f"{text}: temporary tables are not allowed"))
            for hint in table.args.get("hints") or []:
                names = {e.sql(dialect=self.read).upper() for e in hint.expressions}
                locking = sorted(names - _READ_HINTS)
                if locking:
                    out.append(Violation("table_hint",
                                         f"{text}: table hints {', '.join(locking)} "
                                         "are not allowed (only NOLOCK)"))
        for select in tree.find_all(exp.Select):
            if select is not tree and select.args.get("options"):
                out.append(Violation("query_option", "query hints (OPTION …) are not allowed"))
        return out
