#!/usr/bin/env bash
set -euo pipefail
mkdir -p evidence bin
go build -o bin/control ./cmd/control
go build -o bin/previewmesh ./cmd/previewmesh
common=(--repository-id "$REPOSITORY_ID" --source-repository "$SOURCE_REPOSITORY" --pr "$PR_NUMBER")
bin/control resolve "${common[@]}" > evidence/registration.json
run_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
preview_url="http://pm-r${REPOSITORY_ID}-pr${PR_NUMBER}.preview.test"
export ATTEMPT_SHA="$BUILT_SHA" OUTCOME=failure REPORTING_RESULT=not_attempted
finish() {
  code=$?
  trap - EXIT
  status=failure; description='Preview attempt failed'; target="$run_url"
  case "$OUTCOME" in
    ready) status=success; description='Preview ready at verified revision'; target="$preview_url";;
    removed) status=success; description='Preview removed';;
    superseded) description='Preview attempt superseded by newer revision';;
  esac
  details=(--build-state 'not attempted')
  if [ -n "$BUILT_IMAGE" ]; then details=(--build-state success); fi
  if [ -f evidence/cleanup.json ]; then
    details+=(--result-file evidence/cleanup.json)
  elif [ -f evidence/deploy.json ]; then
    details+=(--result-file evidence/deploy.json)
  fi
  if bin/control status "${common[@]}" --sha "$ATTEMPT_SHA" --state "$status" --description "$description" --url "$target" --comment --run-url "$run_url" "${details[@]}"; then
    export REPORTING_RESULT=success
  else
    export REPORTING_RESULT=failure
  fi
  # A reporting failure must not make confirmed deletion look unsuccessful.
  python3 - <<'PY'
import json, os
with open('evidence/outcome.json', 'w') as f:
    json.dump({k.lower(): os.environ.get(k, '') for k in ['ATTEMPT_SHA','OUTCOME','REPORTING_RESULT']}, f)
PY
  exit "$code"
}
trap finish EXIT
inspect() {
  env -u GITHUB_OUTPUT bin/control inspect "${common[@]}" > "$1"
}
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
cleanup() {
  bin/previewmesh cleanup "${common[@]}" --sha "$ATTEMPT_SHA" --evidence evidence/local.csv --result-file evidence/cleanup.json
  export OUTCOME=removed
}
inspect evidence/before.json
current_sha=$(field evidence/before.json sha)
if [ -z "$ATTEMPT_SHA" ]; then export ATTEMPT_SHA="$current_sha"; fi
if [ "$(field evidence/before.json state)" = closed ]; then cleanup; exit 0; fi
if [ "$current_sha" != "$BUILT_SHA" ]; then export OUTCOME=superseded; exit 0; fi
port=$(field evidence/before.json port)
bin/previewmesh deploy "${common[@]}" --sha "$BUILT_SHA" --image "$BUILT_IMAGE" --port "$port" --evidence evidence/local.csv --result-file evidence/deploy.json
inspect evidence/after.json
if [ "$(field evidence/after.json state)" = closed ]; then cleanup; exit 0; fi
if [ "$(field evidence/after.json sha)" != "$BUILT_SHA" ]; then export OUTCOME=superseded; exit 0; fi
export OUTCOME=ready
