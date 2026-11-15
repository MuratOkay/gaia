You are judging whether an analyst's interpretation of database columns matches a reference. You receive ONE table: for each column, the reference and the analyst's answer. You do not know how the answer was produced; judge only what it says.

Reference kinds:
- `names`: the column's original name, its table's name and its data type, as the developers wrote them. Assume the developers named the column for what it holds, and judge whether the answer describes what a column with that name, in that table, holds.
- `label`: a subject-matter expert's description of the column. Authoritative: it wins over what the name suggests.
- `ground_truth`: the designer's description of the column. Authoritative.

For every column decide:
- `meaning`:
  - `yes`: the same business meaning. Synonyms, different wording, or a slightly more or less specific description are fine as long as a user asking about the reference would be served by the answer.
  - `partial`: related but materially different: the right entity with the wrong attribute, the right attribute of the wrong entity, or so generic that it does not identify the column (e.g. "amount" for a tax amount, "date" for a delivery date).
  - `no`: a different meaning, or no meaning at all.
- `role_ok`: only when the reference lists `roles`: true if the answer's `role` is one of them (`*` accepts any role), else false. Null when the reference lists no roles.
- `note`: one short sentence on why, especially for `partial` and `no`.

Be strict about meaning and lenient about wording. Do not reward an answer for sounding confident. Answer for EVERY column exactly once, using the exact column name from the reference.

Reply with a single JSON object and nothing else:

{
  "columns": [
    {"column": "order_total", "meaning": "yes", "role_ok": null, "note": "Same amount; 'gross order value' is a synonym."}
  ]
}
