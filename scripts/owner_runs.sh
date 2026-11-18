#!/usr/bin/env bash
# The owner's asynchronous runs, grouped by what they wait on (OWNER_VERIFICATION "Run plan").
#
#   scripts/owner_runs.sh setup         once per delivery: dependencies, migrations, tests, web UI (~10 min)
#   scripts/owner_runs.sh quick         no LLM gateway: queries, retrieval, SQL Server profile (~20 min)
#   scripts/owner_runs.sh gateway       LLM, thinking off: golden run, 3C judging, questions (~1-2 h)
#   scripts/owner_runs.sh gateway-long  LLM, thinking on: the rest of 3C, demo_pg meanings (overnight)
#
# "quick" and one "gateway…" can run at the same time (different terminals). Two gateway
# tracks at once: give each half the gateway's 4 slots, e.g.
#   GAIA_LLM_CONCURRENCY=2 scripts/owner_runs.sh gateway
#   GAIA_LLM_CONCURRENCY=2 scripts/owner_runs.sh gateway-long
#
# Every step writes verification/<id>.out, starting with `make stamp`. A failing step is
# reported and the next one runs; every LLM step is resumable — run the track again and what
# was answered is replayed. Prerequisites: the §9 exports for `make test`, Ollama for
# retrieval, sql1 for SQL Server steps; `make dev-api` is not needed (the chat test, §19b,
# stays manual).
set -u
cd "$(dirname "$0")/.."
OUT=verification
NGE="--table dbo.transactions --with-neighbours"
LABELS=verification/labels_nge_small.csv

run() {
  local id="$1"; shift
  echo "== $id: $*"
  local started=$SECONDS
  { make -s stamp; "$@"; } > "$OUT/$id.out" 2>&1
  local code=$?
  echo "   exit $code in $((SECONDS - started)) s -> $OUT/$id.out"
}

setup() {
  run 20_venv make venv
  run 20_migrate bash -c "cd backend && ../gaia-env/bin/alembic upgrade head"
  if ! grep -q '^GAIA_MASTER_KEY=.\+' .env 2>/dev/null; then
    echo "!! add GAIA_MASTER_KEY to .env first:  make master-key >> .env"
  fi
  run 20_tests make test
  run 20_web bash -c "make web-install && make web-test && make web-build"   # Phase 5
}

quick() {
  # freeze the 3C inputs first: after this, profiling SQL Server again changes nothing in 3C
  run 20_freeze_nge make evaluate-freeze ARGS="--out evaluations/nge_small $NGE"
  for f in infra/pg-target/queries/*.sql; do
    run "20_query_pg_$(basename "$f" .sql)" make query TARGET=demo_pg ARGS="--file $f --rows 5"
  done
  for f in infra/sqlserver/queries/*.sql; do
    run "20_query_mssql_$(basename "$f" .sql)" make query TARGET=demo_mssql ARGS="--file $f --rows 5"
  done
  run 20_retrieval_pg make retrieval-eval TARGET=demo_pg \
      ARGS="--file infra/pg-target/retrieval_questions.csv --out $OUT/20_retrieval_pg.json"
  run 20_retrieval_pg_words make retrieval-eval TARGET=demo_pg \
      ARGS="--file infra/pg-target/retrieval_questions.csv --no-vectors"
  run 20_retrieval_mssql make retrieval-eval TARGET=demo_mssql \
      ARGS="--file infra/sqlserver/retrieval_questions.csv --out $OUT/20_retrieval_mssql.json"
  run 20_profile_mssql make profile TARGET=demo_mssql ARGS="--force --report"
  run 20_datasources make datasource ARGS="list"
}

gateway() {
  # a dated directory: running the track again the same day resumes it (--resume; the run is
  # recorded as mixed or replayed); another day asks the model afresh (D80)
  run 20_golden_pg make golden TARGET=demo_pg \
      ARGS="--file infra/pg-target/golden.yaml --out evaluations/golden_demo_pg_$(date +%Y%m%d) --resume"
  run 20_judge_nge make evaluate-judge \
      ARGS="--out evaluations/nge_small --labels $LABELS --condition B-C --condition B-P"
  run 20_report_nge make evaluate-report ARGS="--out evaluations/nge_small --labels $LABELS"
  run 20_ask_mssql bash -c "make ask TARGET=demo_mssql ARGS=\"--question 'What was the total transaction amount per currency in 2024?' --show-sql\"; \
      make ask TARGET=demo_mssql ARGS=\"--question 'How many customers made at least one transaction?' --show-sql\""
}

gateway_long() {
  run 20_evaluate_nge make evaluate \
      ARGS="--out evaluations/nge_small $NGE --condition N-C --condition N-P --condition B-P-nt"
  run 20_evaluate_pg make evaluate TARGET=demo_pg \
      ARGS="--out evaluations/demo_pg --schema legacy --condition N-P --condition N-P-h --condition B-P --hide rpt.v_legacy_transactions"
  run 20_judge_nge_all make evaluate-judge ARGS="--out evaluations/nge_small --labels $LABELS"
  run 20_report_nge_all make evaluate-report ARGS="--out evaluations/nge_small --labels $LABELS"
  run 20_judge_pg make evaluate-judge ARGS="--out evaluations/demo_pg --labels infra/pg-target/labels.csv"
  run 20_report_pg make evaluate-report ARGS="--out evaluations/demo_pg --labels infra/pg-target/labels.csv"
  run 20_reason_pg make reason TARGET=demo_pg ARGS="--cost-report"
}

case "${1:-}" in
  setup) setup ;;
  quick) quick ;;
  gateway) gateway ;;
  gateway-long) gateway_long ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
