#!/usr/bin/env bash

# Resolve and publish the Discover feed from the Atlas maintenance timer.
# The feed generator delegates editorial research through PMM_FEED_STATUS=NEEDS_AGENT;
# this wrapper gives that request to the same Codex CLI used by nightly enrichment.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
max_agent_passes="${PKG_DISCOVER_FEED_MAX_AGENT_PASSES:-3}"

[[ "${max_agent_passes}" =~ ^[1-9][0-9]*$ ]] || {
  echo "error: PKG_DISCOVER_FEED_MAX_AGENT_PASSES must be a positive integer" >&2
  exit 1
}

cd "${repo_root}"

run_generator() {
  local output_path="$1"
  "${repo_root}/scripts/update-discover-feed" >"${output_path}" 2>&1
  cat "${output_path}"
}

for ((pass = 1; pass <= max_agent_passes; pass++)); do
  output_path="$(mktemp)"
  trap 'rm -f -- "${output_path}"' EXIT
  run_generator "${output_path}"

  if grep -q '^PMM_FEED_STATUS=NEEDS_AGENT$' "${output_path}"; then
    echo "Discover feed requires Codex research (pass ${pass}/${max_agent_passes})"
    codex --model gpt-5.6 --config 'model_reasoning_effort="medium"' \
      --search --ask-for-approval never exec \
      --ephemeral --ignore-user-config --color never \
      --sandbox danger-full-access -C "${repo_root}" \
      "$(cat "${output_path}")"
    rm -f -- "${output_path}"
    trap - EXIT
    continue
  fi

  if grep -q '^PMM_FEED_STATUS=ERROR$' "${output_path}"; then
    echo "error: Discover feed generator reported an error" >&2
    exit 1
  fi

  if grep -q '^PMM_FEED_STATUS=\(NOOP\|COMMITTED\)$' "${output_path}"; then
    rm -f -- "${output_path}"
    trap - EXIT
    "${repo_root}/scripts/update-discover-feed" --check
    [[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
      echo "error: Discover feed left tracked changes after validation" >&2
      exit 1
    }
    "${repo_root}/scripts/publish-discover-feed-atlas.sh"
    exit 0
  fi

  echo "error: Discover feed generator returned an unknown status" >&2
  exit 1
done

echo "error: Discover feed still needs research after ${max_agent_passes} Codex pass(es)" >&2
exit 1
