#!/usr/bin/env bash
# Print compact GitLab activity since the last working day, for a standup.
# Usage: gitlab-activity.sh [--since YYYY-MM-DD]
set -euo pipefail
set +x
shopt -s inherit_errexit

MAX_EVENT_PAGES=3   # 3 x 100 = 300 events
MAX_PIPELINE_CALLS=10
SECTION_CAP=8

die() { printf 'gitlab-activity: %s\n' "$*" >&2; exit 1; }

for cmd in glab jq date; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' not found on PATH"
done

usage() { printf 'Usage: %s [--since YYYY-MM-DD]\n' "${0##*/}" >&2; exit 2; }

since=""
while (($#)); do
  case "$1" in
    --since)
      (($# >= 2)) || usage
      since="$2"; shift 2 ;;
    --since=*) since="${1#--since=}"; shift ;;
    -h|--help) usage ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage ;;
  esac
done

if [[ -n "$since" ]]; then
  if ! [[ "$since" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
    || ! date -d "$since" >/dev/null 2>&1; then
    die "invalid --since date '$since' (want YYYY-MM-DD)"
  fi
else
  # Mon -> Fri, Sun -> Fri, Sat -> Fri, else yesterday.
  case "$(date +%u)" in
    1) since=$(date -d '3 days ago' +%F) ;;
    7) since=$(date -d '2 days ago' +%F) ;;
    *) since=$(date -d 'yesterday' +%F) ;;
  esac
fi
# Events API 'after' is exclusive.
after=$(date -d "$since -1 day" +%F)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
errf="$tmp/stderr"

# glab api exits 0 on HTTP errors, so check stderr as well.
# Returns 0 ok, 4 on HTTP 404, 1 on any other failure.
api_try() {
  local out rc=0
  out=$(glab api "$1" 2>"$errf") || rc=$?
  if grep -q 'HTTP 404' "$errf"; then
    return 4
  fi
  if ((rc != 0)) || grep -Eq 'HTTP [0-9]{3}' "$errf"; then
    return 1
  fi
  printf '%s' "$out"
}

api() {
  local out rc=0
  out=$(api_try "$1") || rc=$?
  if ((rc != 0)); then
    printf 'gitlab-activity: API call failed: %s\n' "$1" >&2
    grep -v 'Invalid token' "$errf" | head -n 5 >&2 || true
    exit 1
  fi
  printf '%s' "$out"
}

# Fetch up to N pages of a list endpoint into $tmp/<name>.json.
# Files, not variables: big responses overflow the argument limit.
api_list() {
  local name="$1" path="$2" pages="${3:-1}" sep page n pf
  [[ "$path" == *\?* ]] && sep='&' || sep='?'
  for ((page = 1; page <= pages; page++)); do
    pf="$tmp/${name}.page${page}"
    api "${path}${sep}per_page=100&page=${page}" >"$pf" || exit 1
    jq -e 'type == "array"' "$pf" >/dev/null 2>&1 \
      || die "unexpected response from $path (not a JSON array)"
    n=$(jq 'length' "$pf")
    if ((n < 100)); then break; fi
  done
  jq -s 'add // []' "$tmp/${name}".page* >"$tmp/${name}.json"
}

me=$(api user | jq -r '.username // empty')
[[ -n "$me" ]] || die "could not read authenticated user (is glab logged in?)"

api_list events "events?after=${after}&sort=desc" "$MAX_EVENT_PAGES"
api_list mine "merge_requests?scope=created_by_me&state=opened"
api_list assigned "merge_requests?scope=assigned_to_me&state=opened"
api_list merged "merge_requests?scope=created_by_me&state=merged&updated_after=${since}T00:00:00Z"

# Resolve project ids to paths; one call per unique id, in this shell.
declare -A proj=()
while read -r pid; do
  [[ -n "$pid" && -z "${proj[$pid]+x}" ]] || continue
  rc=0
  body=$(api_try "projects/${pid}") || rc=$?
  if ((rc == 0)); then
    proj[$pid]=$(jq -r '.path_with_namespace // empty' <<<"$body")
  elif ((rc == 4)); then
    proj[$pid]=""
  else
    printf 'gitlab-activity: API call failed: projects/%s\n' "$pid" >&2
    grep -v 'Invalid token' "$errf" | head -n 5 >&2 || true
    exit 1
  fi
  [[ -n "${proj[$pid]}" ]] || proj[$pid]="project#${pid}"
done < <(jq -r '.[].project_id // empty' "$tmp/events.json" | sort -u)

projmap='{}'
for pid in "${!proj[@]}"; do
  projmap=$(jq -c --arg k "$pid" --arg v "${proj[$pid]}" '. + {($k): $v}' <<<"$projmap")
done

# Pipeline status for my own open MRs (capped).
pipemap='{}'
count=0
while IFS=$'\t' read -r pid iid url; do
  [[ -n "$pid" ]] || continue
  ((count++ < MAX_PIPELINE_CALLS)) || break
  status=$(api "projects/${pid}/merge_requests/${iid}" \
    | jq -r '.head_pipeline.status // "none"')
  pipemap=$(jq -c --arg k "$url" --arg v "$status" '. + {($k): $v}' <<<"$pipemap")
done < <(jq -r '.[] | select(.web_url | test("deletion_scheduled") | not)
  | [.project_id, .iid, .web_url] | @tsv' "$tmp/mine.json")

jq -rn \
  --arg since "$since" --arg me "$me" --argjson cap "$SECTION_CAP" \
  --argjson projs "$projmap" --argjson pipes "$pipemap" \
  --slurpfile ev "$tmp/events.json" --slurpfile mine "$tmp/mine.json" \
  --slurpfile assigned "$tmp/assigned.json" \
  --slurpfile merged "$tmp/merged.json" '
def trunc($n): (. // "") | gsub("\\s+"; " ") | if length > $n then .[0:$n-3] + "..." else . end;
def path: $projs[(.project_id | tostring)] // "project#\(.project_id)";
def noise: test("deletion_scheduled");
def capped: if length > $cap then .[0:$cap] + ["  ... \(length - $cap) more"] else . end;
def section($title; $lines):
  if ($lines | length) > 0 then "\n\($title):", ($lines | capped | .[]) else empty end;
def mrref: .references.full // "\(.web_url)";
def mrpath: mrref | sub("!\\d+$"; "");
$ev[0] as $ev | $mine[0] as $mine | $assigned[0] as $assigned
| $merged[0] as $merged
|
(($mine + $assigned) | unique_by(.web_url)
  | map(select(any(.assignees[]?; .username == $me)))) as $ownedmrs
| ($ownedmrs | map(mrref)) as $ownedkeys
| ($ev | map(. + {p: path}) | map(select(.p | noise | not))) as $e
# Pushes, grouped by project and branch.
| ($e | map(select(.action_name | startswith("pushed")))
      | map(select(.push_data.action != "removed"))
      | group_by([.p, .push_data.ref])
      | map(.[0] as $f
          | ([.[].push_data.commit_count // 0] | add) as $n
          | "  \($f.p) \($f.push_data.ref): \($n) commit\(if $n == 1 then "" else "s" end)"
            + (if $f.push_data.commit_title then " - \($f.push_data.commit_title | trunc(80))" else " (new branch)" end))
  ) as $pushed
# Activity on MRs and issues, keyed by project + type + iid.
| ($e | map(
        if (.target_type == "MergeRequest" or .target_type == "Issue") then
          {p, t: .target_type, iid: .target_iid, act: (.action_name | if . == "accepted" then "merged" else . end),
           title: .target_title, body: null}
        elif (.note.noteable_type == "MergeRequest" or .note.noteable_type == "Issue") then
          {p, t: .note.noteable_type, iid: .note.noteable_iid, act: "commented",
           title: .target_title, body: .note.body}
        else empty end)
  ) as $acts
| ($merged | map(select(.merged_at != null and .merged_at[0:10] >= $since))
      | map({p: mrpath, t: "MergeRequest", iid, act: "merged", title, body: null})
      | map(select(.p | noise | not))) as $mergedacts
| def activity($type; $sigil):
    ($acts + (if $type == "MergeRequest" then $mergedacts else [] end))
    | map(select(.t == $type))
    | group_by([.p, .iid])
    | map(select(
        ([.[].act] | any(. == "opened" or . == "merged" or . == "closed"))
        or ((.[0].p + "!" + (.[0].iid | tostring)) as $k | $ownedkeys | index($k) != null)))
    | map(
        ([.[].act] | unique) as $a
        | ([.[] | select(.act == "commented")] | length) as $nc
        | ([.[] | select(.body != null) | .body] | last) as $b
        | "  \(.[0].p) \($sigil)\(.[0].iid) [\($a | join(", "))\(if $nc > 1 then " x\($nc)" else "" end)] \(.[0].title | trunc(80))"
          + (if $b then " | \"\($b | trunc(60))\"" else "" end));
  activity("MergeRequest"; "!") as $mrs
| activity("Issue"; "#") as $issues
| ($ownedmrs | map(select(.web_url | noise | not))
      | map("  \(mrref) \(.title | trunc(80))\(if .draft then " (draft)" else "" end)"
            + " [pipeline: \($pipes[.web_url] // "?"), \(.detailed_merge_status // "?")] \(.web_url)")
  ) as $open
| "GitLab activity for \($me) since \($since)",
  section("Pushed"; $pushed),
  section("Merge requests"; $mrs),
  section("Issues"; $issues),
  section("Open MRs (assigned to me)"; $open),
  (if ([$pushed, $mrs, $issues, $open] | add | length) == 0 then "\n(no activity found)" else empty end)
'
