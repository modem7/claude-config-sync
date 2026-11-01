#!/usr/bin/env bash
# Per-machine profiles.
#
# A profile lets one machine differ from the shared config in agents, hooks,
# skills and CLAUDE.md without changing the shared files:
#
#   machines/<profile>/agents/<entry>            replaces/adds ~/.claude/agents/<entry>
#   machines/<profile>/hooks/<entry>             replaces/adds ~/.claude/hooks/<entry>
#   machines/<profile>/skills/<entry>/           replaces/adds ~/.agents/skills/<entry>/
#   machines/<profile>/<kind>/<entry>.disabled   hides the shared <entry> on this machine
#   machines/<profile>/CLAUDE.append.md          appended to ~/.claude/CLAUDE.md
#
# The profile name is PROFILE= in machines/<hostname>.conf, else the
# hostname. An entry is a top-level name in the kind directory; a whole
# skill directory is replaced, never merged.
#
#   apply:   after the shared files are mirrored into place, copy each
#            profile entry over the live path, delete disabled entries, and
#            append CLAUDE.append.md after a marker line. A snapshot of what
#            was applied is kept in a local-only stamp.
#   capture: every name the profile or the stamp mentions is owned by this
#            machine, so it never flows into the shared copy (and the shared
#            copy is never deleted for it). A live owned entry is copied back
#            into the profile only when it differs from the stamp's snapshot,
#            i.e. it was edited locally since the last apply. No snapshot
#            means "not applied yet", not "edited": the profile is left as
#            it is (the same idea as overlay_split's override_before).

PROFILE_KINDS=(agents hooks skills)
PROFILE_APPEND_MARKER='<!-- claude-sync:profile-append -->'

# Local-only record of the last applied profile. Like the other stamps it
# lives in CLAUDE_HOME but outside everything sync_capture copies, so it's
# never synced. Laid out like machines/<profile>/, plus .profile-name.
profile_stamp_dir() {
  echo "$1/.claude-sync-applied-profile"
}

profile_valid_name() {
  local name="$1"
  [[ "${name}" =~ ^[A-Za-z0-9._-]+$ ]] && [ "${name}" != "." ] && [ "${name}" != ".." ]
}

# profile_resolve_name REPO_DIR HOSTNAME  -> this machine's profile name
profile_resolve_name() {
  local repo_dir="$1"
  local hostname="$2"
  local name
  if name="$(read_machine_profile "$(machine_conf_path "${repo_dir}" "${hostname}")")"; then
    echo "${name}"
  else
    echo "${hostname}"
  fi
}

profile_dir() {
  echo "$1/machines/$2"
}

# Where the shared copy of KIND lives in the repo, and where it lives locally.
profile_shared_dir() {
  case "$2" in
    agents) echo "$1/dot-claude/agents" ;;
    hooks) echo "$1/dot-claude/hooks" ;;
    skills) echo "$1/dot-agents/skills" ;;
  esac
}

profile_live_dir() {  # CLAUDE_HOME AGENTS_HOME KIND
  case "$3" in
    agents) echo "$1/agents" ;;
    hooks) echo "$1/hooks" ;;
    skills) echo "$2/skills" ;;
  esac
}

# Top-level names in DIR, sorted, without .gitkeep.
_profile_names() {
  local dir="$1" p
  [ -d "${dir}" ] || return 0
  for p in "${dir}"/* "${dir}"/.[!.]* "${dir}"/..?*; do
    [ -e "${p}" ] || [ -L "${p}" ] || continue
    [ "${p##*/}" = ".gitkeep" ] && continue
    echo "${p##*/}"
  done
}

# profile_entries DIR KIND  -> names DIR/KIND replaces or adds
profile_entries() {
  local name
  while IFS= read -r name; do
    case "${name}" in *.disabled) ;; *) echo "${name}" ;; esac
  done < <(_profile_names "$1/$2")
}

# profile_disabled DIR KIND  -> names DIR/KIND hides
profile_disabled() {
  local name
  while IFS= read -r name; do
    case "${name}" in *.disabled) echo "${name%.disabled}" ;; esac
  done < <(_profile_names "$1/$2")
}

# profile_owned_names REPO_DIR CLAUDE_HOME PROFILE KIND
# Entries and markers in the profile, plus everything the last apply
# touched (it may have been removed from the profile since).
profile_owned_names() {
  local pdir stamp
  pdir="$(profile_dir "$1" "$3")"
  stamp="$(profile_stamp_dir "$2")"
  {
    profile_entries "${pdir}" "$4"
    profile_disabled "${pdir}" "$4"
    profile_entries "${stamp}" "$4"
    profile_disabled "${stamp}" "$4"
  } | LC_ALL=C sort -u
}

# True when a profile or a stamp is in play, i.e. the feature is in use.
profile_in_use() {  # REPO_DIR CLAUDE_HOME PROFILE
  [ -d "$(profile_dir "$1" "$3")" ] || [ -d "$(profile_stamp_dir "$2")" ]
}

# Same content, and for plain files the same executable bit.
_profile_same() {
  local a="$1" b="$2"
  if [ -d "${a}" ] && [ -d "${b}" ]; then
    diff -rq "${a}" "${b}" >/dev/null 2>&1
  elif [ -f "${a}" ] && [ -f "${b}" ]; then
    cmp -s "${a}" "${b}" || return 1
    if [ -x "${a}" ]; then [ -x "${b}" ]; else [ ! -x "${b}" ]; fi
  else
    return 1
  fi
}

# Byte offset of the marker line in FILE, or fail if there isn't one.
_profile_marker_offset() {
  local line
  [ -f "$1" ] || return 1
  line="$(grep -m1 -bxF -- "${PROFILE_APPEND_MARKER}" "$1")" || return 1
  echo "${line%%:*}"
}

# FILE without the append block: everything before the newline that
# precedes the marker line. The exact inverse of the append in profile_apply,
# whether or not the shared file ends in a newline.
profile_strip_claude_md() {  # FILE OUT
  local offset
  if offset="$(_profile_marker_offset "$1")"; then
    if [ "${offset}" -gt 0 ]; then head -c "$((offset - 1))" "$1" > "$2"; else : > "$2"; fi
  else
    cp "$1" "$2"
  fi
}

# The append block's text (after the marker line), byte for byte.
profile_append_block() {  # FILE OUT
  local offset
  offset="$(_profile_marker_offset "$1")" || return 1
  tail -c +"$((offset + ${#PROFILE_APPEND_MARKER} + 2))" "$1" > "$2"
}

# profile_unapply CLAUDE_HOME AGENTS_HOME
# Runs before sync_apply: removes the live copies of the entries the last
# apply put in place, so sync_apply restores the shared version afresh.
# Otherwise rsync's size-and-mtime check can skip a shared file that has the
# same size as the profile copy and was checked out in the same second.
profile_unapply() {
  local claude_home="$1"
  local agents_home="$2"
  local stamp kind live name
  stamp="$(profile_stamp_dir "${claude_home}")"
  [ -d "${stamp}" ] || return 0
  for kind in "${PROFILE_KINDS[@]}"; do
    live="$(profile_live_dir "${claude_home}" "${agents_home}" "${kind}")"
    while IFS= read -r name; do
      rm -rf "${live:?}/${name}"
    done < <(profile_entries "${stamp}" "${kind}")
  done
}

# profile_apply REPO_DIR CLAUDE_HOME AGENTS_HOME PROFILE
profile_apply() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local profile="$4"
  local pdir stamp kind live name tmp claude_md
  pdir="$(profile_dir "${repo_dir}" "${profile}")"
  stamp="$(profile_stamp_dir "${claude_home}")"
  claude_md="${claude_home}/CLAUDE.md"

  # Drop the record first: if this apply dies half way, capture then sees
  # "not applied yet" and leaves the profile alone.
  rm -rf "${stamp}" "${stamp}.tmp"

  # A block left from an earlier apply (sync_apply only rewrites CLAUDE.md
  # when the repo has one) must never be appended twice.
  if [ -f "${claude_md}" ] && _profile_marker_offset "${claude_md}" >/dev/null; then
    tmp="$(mktemp)"
    profile_strip_claude_md "${claude_md}" "${tmp}"
    cat "${tmp}" > "${claude_md}"
    rm -f "${tmp}"
  fi

  [ -d "${pdir}" ] || return 0

  for kind in "${PROFILE_KINDS[@]}"; do
    live="$(profile_live_dir "${claude_home}" "${agents_home}" "${kind}")"
    while IFS= read -r name; do
      mkdir -p "${live}"
      rm -rf "${live:?}/${name}"
      cp -a "${pdir}/${kind}/${name}" "${live}/${name}"
    done < <(profile_entries "${pdir}" "${kind}")
    # Markers go last, so a marker wins over an entry of the same name.
    while IFS= read -r name; do
      rm -rf "${live:?}/${name}"
    done < <(profile_disabled "${pdir}" "${kind}")
  done

  if [ -f "${pdir}/CLAUDE.append.md" ]; then
    { printf '\n%s\n' "${PROFILE_APPEND_MARKER}"; cat "${pdir}/CLAUDE.append.md"; } >> "${claude_md}"
  fi

  cp -a "${pdir}" "${stamp}.tmp"
  printf '%s\n' "${profile}" > "${stamp}.tmp/.profile-name"
  mv "${stamp}.tmp" "${stamp}"
}

# _profile_writeback REPO_DIR CLAUDE_HOME AGENTS_HOME PROFILE MODE
# Finds owned entries (and the append block) edited locally since the last
# apply. MODE "check" returns 0 at the first one; "write" copies each back
# into the profile; "write-stamp" also refreshes the stamp so the live copy
# counts as applied. Returns 0 when there was at least one, 1 otherwise.
_profile_writeback() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local profile="$4"
  local mode="$5"
  local pdir stamp kind live name found=1 tmp
  pdir="$(profile_dir "${repo_dir}" "${profile}")"
  stamp="$(profile_stamp_dir "${claude_home}")"
  [ -d "${pdir}" ] && [ -d "${stamp}" ] || return 1
  # A snapshot of some other profile is no record of this one.
  [ "$(cat "${stamp}/.profile-name" 2>/dev/null)" = "${profile}" ] || return 1

  for kind in "${PROFILE_KINDS[@]}"; do
    live="$(profile_live_dir "${claude_home}" "${agents_home}" "${kind}")"
    # Only names still in the profile: one removed from it since the last
    # apply reverts to the shared version on the next apply.
    while IFS= read -r name; do
      [ -e "${stamp}/${kind}/${name}" ] || continue
      [ -e "${live}/${name}" ] || continue
      _profile_same "${stamp}/${kind}/${name}" "${live}/${name}" && continue
      found=0
      [ "${mode}" = "check" ] && return 0
      rm -rf "${pdir:?}/${kind}/${name}"
      cp -a "${live}/${name}" "${pdir}/${kind}/${name}"
      if [ "${mode}" = "write-stamp" ]; then
        rm -rf "${stamp:?}/${kind}/${name}"
        cp -a "${live}/${name}" "${stamp}/${kind}/${name}"
      fi
    done < <(profile_entries "${pdir}" "${kind}")
  done

  if [ -f "${pdir}/CLAUDE.append.md" ] && [ -f "${stamp}/CLAUDE.append.md" ]; then
    tmp="$(mktemp)"
    if profile_append_block "${claude_home}/CLAUDE.md" "${tmp}" \
       && ! cmp -s "${tmp}" "${stamp}/CLAUDE.append.md"; then
      found=0
      if [ "${mode}" = "check" ]; then rm -f "${tmp}"; return 0; fi
      cat "${tmp}" > "${pdir}/CLAUDE.append.md"
      [ "${mode}" = "write-stamp" ] && cat "${tmp}" > "${stamp}/CLAUDE.append.md"
    fi
    rm -f "${tmp}"
  fi
  return "${found}"
}

# True when an owned entry or the append block was edited locally since the
# last apply, so a re-apply would discard it.
profile_has_local_edits() {  # REPO_DIR CLAUDE_HOME AGENTS_HOME PROFILE
  _profile_writeback "$1" "$2" "$3" "$4" check
}

# profile_capture REPO_DIR CLAUDE_HOME AGENTS_HOME MEMORY_DIR PROFILE [UPDATE_STAMP]
# sync_capture without the owned names, CLAUDE.md without the append block,
# and local edits to owned entries written back into the profile.
# UPDATE_STAMP=true (the real capture, not a dry run into a scratch tree)
# also records written-back entries as applied.
profile_capture() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local memory_dir="$4"
  local profile="$5"
  local update_stamp="${6:-false}"
  local excludes kind name mode=write
  excludes="$(mktemp -d)"
  for kind in "${PROFILE_KINDS[@]}"; do
    while IFS= read -r name; do
      # Anchored, so only the top-level entry matches. rsync never deletes
      # an excluded name on the receiving side, so the shared copy stays.
      printf '/%s\n' "${name}"
    done < <(profile_owned_names "${repo_dir}" "${claude_home}" "${profile}" "${kind}") > "${excludes}/${kind}"
  done

  sync_capture "${repo_dir}" "${claude_home}" "${agents_home}" "${memory_dir}" "${excludes}"
  rm -rf "${excludes}"

  if [ -f "${claude_home}/CLAUDE.md" ] && _profile_marker_offset "${claude_home}/CLAUDE.md" >/dev/null; then
    profile_strip_claude_md "${claude_home}/CLAUDE.md" "${repo_dir}/dot-claude/CLAUDE.md"
  fi

  [ "${update_stamp}" = "true" ] && mode=write-stamp
  _profile_writeback "${repo_dir}" "${claude_home}" "${agents_home}" "${profile}" "${mode}" || true
}

# profile_live_mismatches REPO_DIR CLAUDE_HOME AGENTS_HOME PROFILE
# Prints each owned "<kind>/<name>" (and "CLAUDE.md append block") whose
# live state differs from what a clean apply of shared + profile gives.
profile_live_mismatches() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local profile="$4"
  local tmp kind name exp live a b
  tmp="$(mktemp -d)"
  mkdir -p "${tmp}/claude" "${tmp}/agents/skills"
  for kind in "${PROFILE_KINDS[@]}"; do
    exp="$(profile_live_dir "${tmp}/claude" "${tmp}/agents" "${kind}")"
    mkdir -p "${exp}"
    [ -d "$(profile_shared_dir "${repo_dir}" "${kind}")" ] \
      && cp -a "$(profile_shared_dir "${repo_dir}" "${kind}")/." "${exp}/"
  done
  [ -f "${repo_dir}/dot-claude/CLAUDE.md" ] && cp "${repo_dir}/dot-claude/CLAUDE.md" "${tmp}/claude/CLAUDE.md"
  # The stamp goes into the scratch home, never over the real one.
  profile_apply "${repo_dir}" "${tmp}/claude" "${tmp}/agents" "${profile}"

  for kind in "${PROFILE_KINDS[@]}"; do
    exp="$(profile_live_dir "${tmp}/claude" "${tmp}/agents" "${kind}")"
    live="$(profile_live_dir "${claude_home}" "${agents_home}" "${kind}")"
    while IFS= read -r name; do
      if [ ! -e "${exp}/${name}" ] && [ ! -e "${live}/${name}" ]; then continue; fi
      _profile_same "${exp}/${name}" "${live}/${name}" || echo "${kind}/${name}"
    done < <(profile_owned_names "${repo_dir}" "${claude_home}" "${profile}" "${kind}")
  done

  a="${tmp}/block_expected"; b="${tmp}/block_live"
  profile_append_block "${tmp}/claude/CLAUDE.md" "${a}" 2>/dev/null || rm -f "${a}"
  profile_append_block "${claude_home}/CLAUDE.md" "${b}" 2>/dev/null || rm -f "${b}"
  if [ -f "${a}" ] || [ -f "${b}" ]; then
    { [ -f "${a}" ] && [ -f "${b}" ] && cmp -s "${a}" "${b}"; } || echo "CLAUDE.md append block"
  fi
  rm -rf "${tmp}"
}

# profile_dangling_hook_refs CLAUDE_HOME SETTINGS
# Prints each hook command target under CLAUDE_HOME/hooks that settings.json
# refers to but that doesn't exist (e.g. a hook disabled by a profile but
# still wired in settings). Any ".claude/hooks/<name>" reference counts,
# whether written as $HOME/..., ${HOME}/..., ~/... or an absolute path.
profile_dangling_hook_refs() {
  local claude_home="$1"
  local settings="$2"
  local ref
  [ -f "${settings}" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  while IFS= read -r ref; do
    [ -e "${claude_home}/hooks/${ref}" ] || echo "${claude_home}/hooks/${ref}"
  done < <(jq -r '.hooks // {} | .. | objects | select(has("command")) | .command | strings' "${settings}" 2>/dev/null \
    | grep -oE '\.claude/hooks/[A-Za-z0-9._-]+' | sed 's#^\.claude/hooks/##' | LC_ALL=C sort -u)
}

# profile_describe REPO_DIR PROFILE  -> every entry, marker and append
profile_describe() {
  local repo_dir="$1"
  local profile="$2"
  local pdir kind name any=false
  pdir="$(profile_dir "${repo_dir}" "${profile}")"
  for kind in "${PROFILE_KINDS[@]}"; do
    while IFS= read -r name; do
      if [ -e "$(profile_shared_dir "${repo_dir}" "${kind}")/${name}" ]; then
        echo "  ${kind}/${name}  (replaces the shared one)"
      else
        echo "  ${kind}/${name}  (machine-only)"
      fi
      any=true
    done < <(profile_entries "${pdir}" "${kind}")
    while IFS= read -r name; do
      echo "  ${kind}/${name}  (disabled)"
      any=true
    done < <(profile_disabled "${pdir}" "${kind}")
  done
  if [ -f "${pdir}/CLAUDE.append.md" ]; then
    echo "  CLAUDE.append.md  (appended to CLAUDE.md)"
    any=true
  fi
  if [ -f "${repo_dir}/machines/${profile}.settings.json" ]; then
    echo "  ${profile}.settings.json  (settings override; see 'override list')"
    any=true
  fi
  [ "${any}" = true ] || echo "  (nothing: no machines/${profile}/ entries and no settings override)"
}
