-- Refused: function_not_allowed. Would lift the statement timeout inside a read-only transaction.
SELECT set_config('statement_timeout', '0', false)
