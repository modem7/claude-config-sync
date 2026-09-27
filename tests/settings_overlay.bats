#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/settings-overlay.sh"
  REPO="${BATS_TEST_TMPDIR}/repo"
  mkdir -p "${REPO}/machines" "${REPO}/dot-claude"
  SHARED="${REPO}/dot-claude/settings.json"
  OV="${REPO}/machines/host-a.settings.json"
  LOCAL="${BATS_TEST_TMPDIR}/local.json"
  OUT="${BATS_TEST_TMPDIR}/shared_out.json"
  BASE="${BATS_TEST_TMPDIR}/override_before.json"
  cat > "${SHARED}" <<'EOF'
{
  "theme": "dark",
  "enabledPlugins": { "a@m": true, "b@m": true },
  "permissions": { "allow": ["Bash(*)", "Read(*)"] }
}
EOF
}

# jq equality of two files, ignoring formatting and key order
same_json() { jq -e -n --slurpfile x "$1" --slurpfile y "$2" '$x[0] == $y[0]' >/dev/null; }

@test "overlay_merge deep-merges objects, replaces arrays, and deletes null keys" {
  echo '{"enabledPlugins":{"c@m":true,"b@m":null},"permissions":{"allow":["Read(*)"]}}' > "${OV}"
  overlay_merge "${SHARED}" "${OV}" > "${LOCAL}"
  [ "$(jq -c '.enabledPlugins' "${LOCAL}")" = '{"a@m":true,"c@m":true}' ]
  [ "$(jq -c '.permissions.allow' "${LOCAL}")" = '["Read(*)"]' ]
  [ "$(jq -r '.theme' "${LOCAL}")" = "dark" ]
}

@test "overlay_split of an unchanged merge gives back the shared file and leaves the override untouched" {
  echo '{"enabledPlugins":{"c@m":true,"b@m":null}}' > "${OV}"
  cp "${OV}" "${BATS_TEST_TMPDIR}/ov_before"
  overlay_merge "${SHARED}" "${OV}" > "${LOCAL}"
  cp "${OV}" "${BASE}"; overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  same_json "${OUT}" "${SHARED}"
  cmp -s "${OV}" "${BATS_TEST_TMPDIR}/ov_before"
}

@test "overlay_split sends new keys to the shared file" {
  echo '{"enabledPlugins":{"c@m":true}}' > "${OV}"
  overlay_merge "${SHARED}" "${OV}" | jq '.enabledPlugins["d@m"] = true | .model = "opus"' > "${LOCAL}"
  cp "${OV}" "${BASE}"; overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  [ "$(jq -c '.enabledPlugins' "${OUT}")" = '{"a@m":true,"b@m":true,"d@m":true}' ]
  [ "$(jq -r '.model' "${OUT}")" = "opus" ]
  [ "$(jq -c . "${OV}")" = '{"enabledPlugins":{"c@m":true}}' ]
}

@test "overlay_split writes local changes to an owned key back into the override, not the shared file" {
  echo '{"enabledPlugins":{"b@m":false}}' > "${OV}"
  overlay_merge "${SHARED}" "${OV}" | jq '.enabledPlugins["b@m"] = true' > "${LOCAL}"
  cp "${OV}" "${BASE}"; overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  [ "$(jq -c . "${OV}")" = '{"enabledPlugins":{"b@m":true}}' ]
  same_json "${OUT}" "${SHARED}"
}

@test "overlay_split records a locally deleted owned key as null, and a re-added deleted key as its value" {
  echo '{"enabledPlugins":{"c@m":true,"a@m":null}}' > "${OV}"
  overlay_merge "${SHARED}" "${OV}" | jq 'del(.enabledPlugins["c@m"]) | .enabledPlugins["a@m"] = false' > "${LOCAL}"
  cp "${OV}" "${BASE}"; overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  [ "$(jq -c . "${OV}")" = '{"enabledPlugins":{"c@m":null,"a@m":false}}' ]
  same_json "${OUT}" "${SHARED}"
}

@test "a nested override the shared file doesn't have leaves no empty skeleton behind in it" {
  echo '{"extraKnownMarketplaces":{"mkt":{"source":{"source":"github","repo":"o/r"}}}}' > "${OV}"
  overlay_merge "${SHARED}" "${OV}" > "${LOCAL}"
  cp "${OV}" "${BASE}"; overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  same_json "${OUT}" "${SHARED}"
  [ "$(jq -r 'has("extraKnownMarketplaces")' "${OUT}")" = "false" ]
}

@test "a key hand-added to the override (not applied yet) is kept, not mistaken for a local deletion" {
  echo '{}' > "${BASE}"
  cp "${SHARED}" "${LOCAL}"
  echo '{"enabledPlugins":{"c@m":true}}' > "${OV}"
  overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  [ "$(jq -c . "${OV}")" = '{"enabledPlugins":{"c@m":true}}' ]
  same_json "${OUT}" "${SHARED}"
}

@test "a key hand-removed from the override doesn't leak its old value into the shared file" {
  echo '{"enabledPlugins":{"c@m":true}}' > "${BASE}"
  overlay_merge "${SHARED}" "${BASE}" > "${LOCAL}"
  echo '{}' > "${OV}"
  overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  same_json "${OUT}" "${SHARED}"
}

@test "a deleted override file behaves like an emptied one and isn't recreated" {
  echo '{"theme":"light"}' > "${BASE}"
  overlay_merge "${SHARED}" "${BASE}" > "${LOCAL}"
  overlay_split "${LOCAL}" "${SHARED}" "${BASE}" "${OV}" "${OUT}"
  same_json "${OUT}" "${SHARED}"
  [ ! -f "${OV}" ]
}

@test "overlay_add moves a key from the shared file into the override" {
  cp "${SHARED}" "${LOCAL}"
  overlay_add "${REPO}" host-a "${LOCAL}" '.enabledPlugins["b@m"]'
  [ "$(jq -c . "${OV}")" = '{"enabledPlugins":{"b@m":true}}' ]
  [ "$(jq -c '.enabledPlugins' "${SHARED}")" = '{"a@m":true}' ]
}

@test "overlay_add refuses a key that isn't set locally" {
  cp "${SHARED}" "${LOCAL}"
  run overlay_add "${REPO}" host-a "${LOCAL}" '.enabledPlugins["zzz@m"]'
  [ "$status" -eq 1 ]
  [ ! -f "${OV}" ]
}

@test "overlay_remove drops the key, prunes empty objects, and deletes an empty override file" {
  echo '{"enabledPlugins":{"c@m":true},"theme":"light"}' > "${OV}"
  overlay_remove "${REPO}" host-a '.enabledPlugins["c@m"]'
  [ "$(jq -c . "${OV}")" = '{"theme":"light"}' ]
  overlay_remove "${REPO}" host-a '.theme'
  [ ! -f "${OV}" ]
}

@test "overlay_list prints one readable path per owned key" {
  echo '{"enabledPlugins":{"c@m":true},"theme":"light"}' > "${OV}"
  run overlay_list "${REPO}" host-a
  [ "${lines[0]}" = '.enabledPlugins["c@m"] = true' ]
  [ "${lines[1]}" = '.theme = "light"' ]
}

@test "a non-object override file is rejected rather than silently merged" {
  echo '[1,2]' > "${OV}"
  run overlay_merge "${SHARED}" "${OV}"
  [ "$status" -eq 1 ]
}
