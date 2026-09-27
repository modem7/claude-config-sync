#!/usr/bin/env bash
# jq programs are single-quoted on purpose: their $vars are jq variables.
# shellcheck disable=SC2016
# Per-machine settings overrides.
#
# dot-claude/settings.json is shared by every machine. A machine can layer
# machines/<hostname>.settings.json on top of it:
#
#   apply:   local settings.json = shared * override   (jq deep merge:
#            objects merge key by key, arrays and scalars replace, and a
#            null value deletes the key)
#   capture: local settings.json is split back into shared + override.
#            Every leaf the override file mentions is owned by this machine:
#            its shared value is left as it was, and if the local value was
#            changed (e.g. a plugin toggled via /plugin), the new value is
#            written back into the override file instead. Everything else
#            flows to the shared file as usual.
#
# "Leaf" means a path the override file reaches without descending into a
# non-object, so e.g. {"enabledPlugins": {"x@y": true}} owns only
# enabledPlugins["x@y"], not the whole enabledPlugins object.

# jq helpers shared by the programs below.
_OVERLAY_JQ_DEFS='
def _leaf:
  if type == "object" and length > 0
  then keys_unsorted[] as $k | (.[$k] | _leaf) as $p | [$k] + $p
  else [] end;
# Never the root itself: an empty override owns nothing, not everything.
def leafpaths: _leaf | select(length > 0);
def has_path($p):
  try (getpath($p[:-1]) | type == "object" and has($p[-1])) catch false;
'

overlay_path() {
  local repo_dir="$1"
  local hostname="$2"
  echo "${repo_dir}/machines/${hostname}.settings.json"
}

_overlay_require_jq() {
  if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required for per-machine settings overrides ($1) but isn't installed." >&2
    return 1
  fi
}

_overlay_require_object() {
  if ! jq -e 'type == "object"' "$1" >/dev/null 2>&1; then
    echo "$1 must contain a JSON object." >&2
    return 1
  fi
}

# overlay_merge SHARED OVERRIDE  -> merged settings on stdout
overlay_merge() {
  local shared="$1"
  local override="$2"
  _overlay_require_jq "${override}" || return 1
  _overlay_require_object "${override}" || return 1
  jq --indent 2 -n --slurpfile s "${shared}" --slurpfile o "${override}" "${_OVERLAY_JQ_DEFS}"'
    $s[0] as $shared | $o[0] as $ov
    | reduce ($ov | leafpaths | select(. as $p | $ov | getpath($p) == null)) as $p
        ($shared * $ov; delpaths([$p]))
  '
}

# overlay_split LOCAL SHARED_BEFORE OVERRIDE_BEFORE OVERRIDE SHARED_OUT
#
# Writes the shared part of LOCAL to SHARED_OUT and updates OVERRIDE in
# place with any locally changed values of the leaves it owns.
#
#   SHARED_BEFORE    the shared settings as last applied (pre-capture)
#   OVERRIDE_BEFORE  the override the last apply layered on ({} if none
#                    was, or there's no record of it)
#   OVERRIDE         the override as it is now (may be hand-edited since,
#                    or missing)
#
# A local change is judged against what the last apply produced, not
# against OVERRIDE itself. Otherwise a key hand-added to the override file
# (not applied yet, so absent locally) would look like a local deletion,
# and a key hand-removed from it would leak its old value into the shared
# file.
overlay_split() {
  local local_file="$1"
  local shared_before="$2"
  local override_before="$3"
  local override="$4"
  local shared_out="$5"
  _overlay_require_jq "${override}" || return 1

  local ov_now existed=false
  if [ -f "${override}" ]; then
    _overlay_require_object "${override}" || return 1
    ov_now="${override}"
    existed=true
  else
    ov_now="$(mktemp)"
    echo '{}' > "${ov_now}"
  fi

  local result
  result="$(jq -n --slurpfile l "${local_file}" --slurpfile s "${shared_before}" \
      --slurpfile b "${override_before}" --slurpfile o "${ov_now}" "${_OVERLAY_JQ_DEFS}"'
    $l[0] as $local | $s[0] as $shared | $b[0] as $base | $o[0] as $ov
    # What the last apply left at $p: {present, v}.
    | def applied($p):
        if ($base | has_path($p)) then
          (if ($base | getpath($p)) == null then {present: false}
           else {present: true, v: ($base | getpath($p))} end)
        elif ($shared | has_path($p)) then {present: true, v: ($shared | getpath($p))}
        else {present: false} end;
      reduce (([$ov | leafpaths] + [$base | leafpaths]) | unique[]) as $p ({shared: $local, ov: $ov};
        ($local | has_path($p)) as $present
        | ($local | getpath($p)) as $lv
        | applied($p) as $a
        | ($present == $a.present and (($present | not) or $lv == $a.v)) as $unchanged
        # Owned now and edited locally since the last apply: keep the edit,
        # machine-side.
        | (if ($ov | has_path($p)) and ($unchanged | not)
           then .ov |= setpath($p; if $present then $lv else null end) else . end)
        # Owned now or before: the shared value stays as it was.
        | .shared |= (if ($shared | has_path($p))
                      then setpath($p; $shared | getpath($p))
                      else delpaths([$p]) end)
        # Deleting a nested leaf can leave its parents behind as empty
        # objects (e.g. {"mkt": {"source": {}}}); drop any that the shared
        # file never had, so they do not leak into it.
        | .shared |= reduce range(($p | length) - 1; 0; -1) as $i (.;
            if has_path($p[:$i]) and (getpath($p[:$i]) | type == "object" and length == 0)
               and (($shared | has_path($p[:$i])) | not)
            then delpaths([$p[:$i]]) else . end))
  ')" || { [ "${existed}" = true ] || rm -f "${ov_now}"; return 1; }
  [ "${existed}" = true ] || rm -f "${ov_now}"

  jq --indent 2 '.shared' <<<"${result}" > "${shared_out}"
  local new_ov
  new_ov="$(jq --indent 2 '.ov' <<<"${result}")"
  # Only (re)write the override file when it actually changed, so a no-op
  # sync doesn't reformat a hand-written file or create an empty one.
  if [ "${existed}" = true ]; then
    jq -e --argjson a "${new_ov}" '. == $a' "${override}" >/dev/null \
      || printf '%s\n' "${new_ov}" > "${override}"
  elif [ "$(jq -c . <<<"${new_ov}")" != "{}" ]; then
    printf '%s\n' "${new_ov}" > "${override}"
  fi
}

# overlay_add REPO_DIR HOSTNAME LOCAL_SETTINGS JQ_PATH
# Makes JQ_PATH (e.g. '.enabledPlugins["x@y"]') machine-specific: records
# its current local value in this machine's override file and removes it
# from the shared settings, so other machines drop it on their next sync.
overlay_add() {
  local repo_dir="$1"
  local hostname="$2"
  local local_settings="$3"
  local expr="$4"
  local override shared path_json
  override="$(overlay_path "${repo_dir}" "${hostname}")"
  shared="${repo_dir}/dot-claude/settings.json"
  _overlay_require_jq "${override}" || return 1

  path_json="$(jq -cn "path(${expr})" 2>/dev/null)" || {
    echo "Not a valid jq path: ${expr}  (example: '.enabledPlugins[\"name@marketplace\"]')" >&2
    return 1
  }
  if ! jq -e --argjson p "${path_json}" "${_OVERLAY_JQ_DEFS}"'has_path($p)' "${local_settings}" >/dev/null; then
    echo "${expr} isn't set in ${local_settings}; set it locally first." >&2
    return 1
  fi

  [ -f "${override}" ] || echo '{}' > "${override}"
  _overlay_require_object "${override}" || return 1
  local tmp
  tmp="$(mktemp)"
  jq --indent 2 --argjson p "${path_json}" --slurpfile l "${local_settings}" \
    'setpath($p; $l[0] | getpath($p))' "${override}" > "${tmp}" && mv "${tmp}" "${override}"
  jq --indent 2 --argjson p "${path_json}" 'delpaths([$p])' "${shared}" > "${tmp}" && mv "${tmp}" "${shared}"
}

# overlay_remove REPO_DIR HOSTNAME JQ_PATH
# Stops overriding JQ_PATH on this machine: it takes the shared value (or
# disappears, if the shared settings don't have it) on the next apply.
overlay_remove() {
  local repo_dir="$1"
  local hostname="$2"
  local expr="$3"
  local override path_json tmp
  override="$(overlay_path "${repo_dir}" "${hostname}")"
  _overlay_require_jq "${override}" || return 1
  [ -f "${override}" ] || { echo "No overrides for ${hostname}." >&2; return 1; }

  path_json="$(jq -cn "path(${expr})" 2>/dev/null)" || {
    echo "Not a valid jq path: ${expr}" >&2
    return 1
  }
  if ! jq -e --argjson p "${path_json}" "${_OVERLAY_JQ_DEFS}"'has_path($p)' "${override}" >/dev/null; then
    echo "${expr} isn't overridden on ${hostname}." >&2
    return 1
  fi
  tmp="$(mktemp)"
  # Drop the leaf, then prune any objects it leaves empty.
  jq --indent 2 --argjson p "${path_json}" '
    delpaths([$p])
    | reduce range(($p | length) - 1; 0; -1) as $i (.;
        if (getpath($p[:$i]) | type == "object" and length == 0) then delpaths([$p[:$i]]) else . end)
  ' "${override}" > "${tmp}" && mv "${tmp}" "${override}"
  if jq -e 'length == 0' "${override}" >/dev/null; then
    rm -f "${override}"
  fi
}

# overlay_list REPO_DIR HOSTNAME  -> one "path = value" line per owned leaf
overlay_list() {
  local repo_dir="$1"
  local hostname="$2"
  local override
  override="$(overlay_path "${repo_dir}" "${hostname}")"
  if [ ! -f "${override}" ]; then
    echo "No overrides for ${hostname}."
    return 0
  fi
  _overlay_require_jq "${override}" || return 1
  jq -r "${_OVERLAY_JQ_DEFS}"'
    . as $ov | leafpaths as $p
    | ($p | map(if test("^[A-Za-z_][A-Za-z0-9_]*$") then "." + . else "[\"" + . + "\"]" end) | join("")) + " = " + ($ov | getpath($p) | tojson)
  ' "${override}"
}
