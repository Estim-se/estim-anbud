#!/bin/bash
# The exam (`anbud eval exam <co>`: both splits, prospective then held-out, in one invocation), detached from the
# session that starts it. Under scoring plan v3.1 an attempt started and never frozen is "no prediction" and is never
# run again, so a paid exam must not die with its parent: a usage limit, a stopped session or a closed terminal. It
# runs in a session of its own, with stdin closed, under `caffeinate -i`, which blocks idle sleep while it runs.
# Lid-close sleep can't be blocked from here: the Mac stays open and on power for the run.
#
#   scripts/exam-run.sh preflight <co> <commit> [--checkout DIR]
#   scripts/exam-run.sh start <co> <commit> [--budget USD] [--checkout DIR]
#   scripts/exam-run.sh status <run folder>
#
# <commit> is the merged commit the last $0 re-pass ran on. --checkout is the clean checkout at it that runs the
# exam (default: this script's own). Without --budget, `start` runs the $0 pass; with it, the paid exam
# (`--paid --budget USD`, the whole exam's budget over both splits and every pass).
#
# Preflight prints states and counts only, and `start` refuses unless every check passes:
#   power      on AC power (pmset)
#   disk       at least MIN_FREE_GB free on the data root's volume
#   checkout   no changed or untracked file in the checkout
#   commit     the checkout's HEAD is <commit>
#   main       origin/main is <commit>, after a fetch
#   key        the workspace's Keychain entry exists: its exit code only, the key is never read
#   eval lane  ANBUD_EVAL_LANE=1 in this environment (the exam refuses without it)
#   running    no other pass started here is still running
#
# Each invocation gets a folder outside the sealed storage, predictions/<co>/exam-runs/<stamp>/:
#   log      the exam's own output: aggregates only, and no figure of the paid runs before the exam's one total
#            (the engines' output stays in sealed storage)
#   pid      the detached process; it lives as long as the exam
#   exit     the exam's exit code, written when it ends
#   command  and commit: what ran
#
# `status` prints the exam's state, its exit code, the log's line count and the exam's own status lines, and of a
# traceback only the error's type, never its message.
#
# Before the paid exam: run `start` without --budget (the $0 pass), kill the shell that started it, and check with
# `status` that the exam still finished and wrote its exit code and log. `launch` is the detach step on its own,
# without the preflight, for this script's tests.
set -euo pipefail

MIN_FREE_GB=${MIN_FREE_GB:-10}
# The exam's own status lines. Each split prints only its header, the prospective deadlines and the scoring plan; every
# other count is one total over both splits, printed once after both (`both splits`, `dry runs, both splits`).
STATUS_LINES='^(anbud eval|anbud: |deadlines:|dry runs, both splits|scoring plan:|paid exam|both splits|size checks|scaled stops|estimate:|estimate id:|warning:)'

die() {
  echo "exam-run: $*" >&2
  exit 2
}

data_root() {
  local root=${ANBUD_DATA_ROOT:-$HOME/estim-anbud-data}
  echo "${root/#\~/$HOME}"
}

check_company() {
  [[ $1 =~ ^co-[0-9]{3}$ ]] || die "a company id is co-NNN"
}

FAILS=0
report() { # name, 1 when it passed, what was found
  if [ "$2" = 1 ]; then
    echo "preflight: $1 ok ($3)"
  else
    echo "preflight: $1 FAILED ($3)"
    FAILS=$((FAILS + 1))
  fi
}

alive() { # a run folder whose pass is still running
  local pid
  [ -f "$1/pid" ] && [ ! -f "$1/exit" ] && pid=$(cat "$1/pid") && kill -0 "$pid" 2>/dev/null
}

preflight() { # co, commit, checkout
  local co=$1 commit=$2 checkout=$3 root source free changed head main expected names code running=0
  root=$(data_root)
  FAILS=0

  if command -v pmset >/dev/null 2>&1; then
    source=$(pmset -g batt 2>/dev/null | head -1 || true)
    if [[ $source == *"AC Power"* ]]; then report power 1 "AC"; else report power 0 "on battery"; fi
  else
    report power 0 "pmset not found"
  fi

  free=$(df -Pk "$root" 2>/dev/null | awk 'NR == 2 { print int($4 / 1048576) }' || true)
  if [ "${free:-0}" -ge "$MIN_FREE_GB" ]; then flag=1; else flag=0; fi
  report disk "$flag" "${free:-0} GB free, at least $MIN_FREE_GB"

  if changed=$(git -C "$checkout" status --porcelain 2>/dev/null); then
    changed=$(printf '%s' "$changed" | grep -c . || true)
  else
    changed="not a git checkout:"
  fi
  if [ "$changed" = 0 ]; then flag=1; else flag=0; fi
  report checkout "$flag" "$changed changed or untracked files"

  expected=$(git -C "$checkout" rev-parse --verify --quiet "$commit^{commit}" || true)
  head=$(git -C "$checkout" rev-parse HEAD 2>/dev/null || true)
  if [ -n "$expected" ] && [ "$head" = "$expected" ]; then flag=1; else flag=0; fi
  report commit "$flag" "HEAD ${head:0:7}, expected ${expected:0:7}"

  git -C "$checkout" fetch --quiet origin main 2>/dev/null || true
  main=$(git -C "$checkout" rev-parse --verify --quiet origin/main || true)
  if [ -n "$expected" ] && [ "$main" = "$expected" ]; then flag=1; else flag=0; fi
  report main "$flag" "origin/main ${main:0:7}"

  # The Keychain service and the workspace (its account) as the harness reads them; neither is a secret.
  names=$(cd "$checkout" && uv run --quiet --frozen python -c \
    'from anbud.config import load_config; from anbud.model_client import KEYCHAIN_SERVICE as s; print(s, load_config().workspace)' \
    2>/dev/null | tail -1 || true)
  [[ $names =~ ^[a-z0-9-]+\ [a-z0-9-]+$ ]] || names="unknown unknown"
  code=0
  # Without -w or -g the entry's secret is never read; only whether it exists, by the exit code.
  security find-generic-password -s "${names% *}" -a "${names#* }" >/dev/null 2>&1 || code=$?
  if [ "$code" = 0 ]; then flag=1; else flag=0; fi
  report key "$flag" "Keychain entry for workspace ${names#* }: exit $code"

  if [ "${ANBUD_EVAL_LANE:-}" = 1 ]; then flag=1; else flag=0; fi
  report "eval lane" "$flag" "ANBUD_EVAL_LANE=${ANBUD_EVAL_LANE:-unset}"

  for dir in "$root/predictions/$co/exam-runs"/*/; do
    if [ -d "$dir" ] && alive "$dir"; then running=$((running + 1)); fi
  done
  if [ "$running" = 0 ]; then flag=1; else flag=0; fi
  report running "$flag" "$running exams running"

  echo "preflight: $FAILS failed"
  [ "$FAILS" = 0 ]
}

launch() { # run folder, checkout, then the arguments to `anbud`
  local dir=$1 checkout=$2 pid
  shift 2
  mkdir -p "$dir"
  chmod 700 "$dir"
  local keep=()
  command -v caffeinate >/dev/null 2>&1 && keep=(caffeinate -i)
  printf '%s\n' "anbud $*" >"$dir/command"
  # perl's setsid puts the pass in a session of its own, so nothing that ends the parent's session or process group
  # reaches it; nohup and the closed stdin keep a closed terminal from ending it. The exit code is written last.
  EXAM_LOG="$dir/log" EXAM_EXIT="$dir/exit" nohup perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV or die "exec: $!"' \
    ${keep[@]+"${keep[@]}"} /bin/sh -c 'cd "$0" || exit 97; uv run --quiet --frozen anbud "$@" >"$EXAM_LOG" 2>&1; echo $? >"$EXAM_EXIT"' \
    "$checkout" "$@" </dev/null >/dev/null 2>&1 &
  pid=$!
  echo "$pid" >"$dir/pid"
  echo "exam-run: started, pid $pid, folder $dir"
}

status() { # run folder
  local dir=$1 state code lines kept error
  [ -f "$dir/pid" ] || die "no pid in that folder"
  if alive "$dir"; then state=running; else state=ended; fi
  code=$(cat "$dir/exit" 2>/dev/null || echo none)
  lines=$(wc -l <"$dir/log" 2>/dev/null | tr -d ' ' || echo 0)
  kept=$(grep -cE "$STATUS_LINES" "$dir/log" 2>/dev/null || true)
  echo "status: $state; exit $code; log ${lines:-0} lines, ${kept:-0} status lines"
  grep -E "$STATUS_LINES" "$dir/log" 2>/dev/null || true
  if grep -q '^Traceback ' "$dir/log" 2>/dev/null; then
    error=$(grep -E '^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Interrupt|Exit)\b' "$dir/log" | tail -1 | cut -d: -f1)
    echo "status: a traceback, ending in ${error:-an unnamed error} (its message stays in the log)"
  fi
}

command=${1:-}
[ $# -gt 0 ] && shift
case "$command" in
  preflight)
    [ $# -ge 2 ] || die "preflight <co> <commit> [--checkout DIR]"
    co=$1 commit=$2 checkout=$(cd "$(dirname "$0")/.." && pwd)
    shift 2
    [ "${1:-}" = --checkout ] && checkout=$(cd "$2" && pwd)
    check_company "$co"
    preflight "$co" "$commit" "$checkout"
    ;;
  start)
    [ $# -ge 2 ] || die "start <co> <commit> [--budget USD] [--checkout DIR]"
    co=$1 commit=$2 budget="" checkout=$(cd "$(dirname "$0")/.." && pwd)
    shift 2
    while [ $# -gt 0 ]; do
      case "$1" in
        --budget) budget=$2 && shift 2 ;;
        --checkout) checkout=$(cd "$2" && pwd) && shift 2 ;;
        *) die "unknown option $1" ;;
      esac
    done
    check_company "$co"
    [ -z "$budget" ] || [[ $budget =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "--budget is USD, a number"
    preflight "$co" "$commit" "$checkout" || die "preflight failed; nothing started"
    runs="$(data_root)/predictions/$co/exam-runs"
    mkdir -p "$runs"
    chmod 700 "$runs"
    dir="$runs/$(date -u +%Y%m%dT%H%M%SZ)"
    [ ! -e "$dir" ] || die "that run folder exists already"
    args=(eval exam "$co")
    [ -z "$budget" ] || args+=(--paid --budget "$budget")
    launch "$dir" "$checkout" "${args[@]}"
    git -C "$checkout" rev-parse HEAD >"$dir/commit"
    ;;
  launch)
    [ $# -ge 3 ] || die "launch <run folder> <checkout> <anbud arguments...>"
    launch "$@"
    ;;
  status)
    [ $# -eq 1 ] || die "status <run folder>"
    status "$1"
    ;;
  *)
    die "preflight, start or status (see the top of this script)"
    ;;
esac
