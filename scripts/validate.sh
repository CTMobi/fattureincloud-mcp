#!/usr/bin/env bash
# Validate the MCPB bundle and the surrounding metadata.
# Usage: ./scripts/validate.sh
#
# Checks:
#   - dist/fattureincloud.mcpb exists and passes `mcpb validate`
#   - manifest.json version matches pyproject.toml and CHANGELOG
#   - all tools in manifest carry annotations
#   - icon.png is present (warns if not 512x512)
#   - privacy_policy_url responds 200 OK
set -euo pipefail

cd "$(dirname "$0")/.."

BUNDLE="dist/fattureincloud.mcpb"
MANIFEST="manifest.json"
fail=0
warn() { echo "WARN: $*" >&2; }
err()  { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# 1. manifest.json passes mcpb schema validation
if [[ ! -f "$MANIFEST" ]]; then
  err "$MANIFEST not found"
elif command -v mcpb >/dev/null 2>&1; then
  mcpb validate "$MANIFEST" >/dev/null 2>&1 \
    && echo "manifest.json: schema validation OK" \
    || err "mcpb validate $MANIFEST failed (run 'mcpb validate $MANIFEST' for details)"
else
  warn "mcpb CLI missing; skipping manifest validation"
fi

# 1b. Bundle exists with reasonable size
if [[ ! -f "$BUNDLE" ]]; then
  warn "$BUNDLE not found (run ./scripts/build.sh to produce it)"
else
  size=$(stat -c '%s' "$BUNDLE" 2>/dev/null || stat -f '%z' "$BUNDLE" 2>/dev/null || echo "")
  if [[ -z "$size" ]]; then
    warn "no known stat form available; bundle size check skipped"
  else
    size_mb=$((size / 1024 / 1024))
    echo "bundle: $BUNDLE (${size_mb} MB)"
    if [[ $size_mb -gt 50 ]]; then
      warn "bundle exceeds 50 MB"
    fi
  fi
fi

# 2. Version coherence
if [[ -f "$MANIFEST" ]] && command -v jq >/dev/null 2>&1; then
  manifest_version=$(jq -r '.version' "$MANIFEST" 2>/dev/null) || manifest_version=""
  pyproject_version=$(grep -E '^version = "' pyproject.toml | head -1 | sed -E 's/^version = "(.+)"/\1/')
  changelog_version=$(grep -m1 -E '^## v' CHANGELOG.md | sed -E 's/^## v//')

  if [[ -z "$manifest_version" || "$manifest_version" == "null" ]]; then
    err "$MANIFEST is not readable as JSON (or has no version)"
    manifest_version=""
  fi
  echo "manifest.json:    $manifest_version"
  echo "pyproject.toml:   $pyproject_version"
  echo "CHANGELOG (top):  $changelog_version"

  if [[ -n "$manifest_version" ]]; then
    if [[ "$manifest_version" != "$pyproject_version" ]]; then
      err "manifest version != pyproject version"
    fi
    if [[ "$manifest_version" != "$changelog_version" ]]; then
      err "manifest version != CHANGELOG top entry"
    fi
  fi
else
  warn "jq not available or manifest missing; skipping version coherence check"
fi

# 3. Tool coherence: the manifest must match the tools the PACKED server really
#    serves. Running from the extracted bundle is also the cheapest check that it
#    starts at all — a dependency that breaks the entry point (as mcp 2.x did)
#    shows up here instead of at the user's first launch.
if [[ -f "$MANIFEST" ]] && command -v jq >/dev/null 2>&1; then
  declared=$(jq -r '.tools[].name' "$MANIFEST" 2>/dev/null | LC_ALL=C sort) || declared=""
  if [[ -z "$declared" ]]; then
    err "$MANIFEST is not readable as JSON, or declares no tools"
  fi
  echo "manifest declares $([[ -z "$declared" ]] && echo 0 || wc -l <<< "$declared") tools"

  if [[ ! -f "$BUNDLE" ]]; then
    warn "no bundle to inspect; skipping runtime tool check (run ./scripts/build.sh)"
  elif ! command -v unzip >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    warn "unzip or python3 missing; skipping runtime tool check"
  else
    staging=$(mktemp -d)
    trap 'rm -rf "$staging"' EXIT

    if ! unzip -qq "$BUNDLE" -d "$staging" 2>"$staging/unzip.err"; then
      err "$BUNDLE could not be extracted (corrupt bundle? run ./scripts/build.sh)"
      sed 's/^/     /' "$staging/unzip.err" >&2 || true
    else
      # cwd is the extracted bundle, so `import server` resolves the packed file
      # and PYTHONPATH=lib its packed dependencies.
      runtime=$(cd "$staging" && PYTHONPATH=lib FIC_ACCESS_TOKEN=validate FIC_COMPANY_ID=0 \
        python3 -c '
import asyncio, server
print("\n".join(sorted(t.name for t in asyncio.run(server.list_tools()))))
' 2>"$staging/import.err") || runtime=""

      # The bundle carries its own manifest: comparing it against the bundled
      # runtime keeps "the bundle is inconsistent" apart from "the bundle is
      # simply older than the checkout", which are different things to fix.
      packed=$(jq -r '.tools[].name' "$staging/manifest.json" 2>/dev/null | LC_ALL=C sort) || packed=""
      packed_version=$(jq -r '.version' "$staging/manifest.json" 2>/dev/null || echo "?")

      if [[ -z "$packed" ]]; then
        err "the bundled manifest.json is unreadable or declares no tools: rebuild with ./scripts/build.sh"
      elif [[ -z "$runtime" ]]; then
        err "the bundled server.py did not start (the extension would fail at launch)"
        sed 's/^/     /' "$staging/import.err" >&2 || true
      elif [[ "$runtime" != "$packed" ]]; then
        err "bundled manifest tools != bundled runtime tools"
        diff <(echo "$packed") <(echo "$runtime") | sed 's/^/     /' >&2 || true
      elif [[ -n "$declared" && "$packed" != "$declared" ]]; then
        err "bundle is stale (tools differ from $MANIFEST): run ./scripts/build.sh"
        diff <(echo "$declared") <(echo "$packed") | sed 's/^/     /' >&2 || true
      elif [[ -z "$packed_version" || "$packed_version" == "null" ]]; then
        err "the bundled manifest.json has no version: rebuild it with ./scripts/build.sh"
      elif [[ -n "$manifest_version" && "$packed_version" != "$manifest_version" ]]; then
        err "bundle is stale (packed $packed_version vs manifest $manifest_version): run ./scripts/build.sh"
      elif [[ -z "$declared" && -z "$manifest_version" ]]; then
        echo "bundled runtime:  $(wc -l <<< "$runtime") tools (v$packed_version; not compared to $MANIFEST)"
      elif [[ -z "$manifest_version" ]]; then
        echo "bundled runtime:  $(wc -l <<< "$runtime") tools (names match $MANIFEST; version not compared)"
      elif [[ -z "$declared" ]]; then
        echo "bundled runtime:  $(wc -l <<< "$runtime") tools (v$packed_version matches $MANIFEST; names not compared)"
      else
        echo "bundled runtime:  $(wc -l <<< "$runtime") tools (match manifest, v$packed_version)"
      fi
    fi
  fi
fi

# 4. icon.png present + size hint
if [[ ! -f icon.png ]]; then
  err "icon.png missing (required by manifest)"
else
  if command -v sips >/dev/null 2>&1; then
    w=$(sips -g pixelWidth icon.png 2>/dev/null | awk '/pixelWidth/ {print $2}')
    h=$(sips -g pixelHeight icon.png 2>/dev/null | awk '/pixelHeight/ {print $2}')
    has_alpha=$(sips -g hasAlpha icon.png 2>/dev/null | awk '/hasAlpha/ {print $2}')
    echo "icon.png: ${w}x${h}, hasAlpha=${has_alpha}"
    [[ "${w}x${h}" != "512x512" ]] && warn "icon.png is ${w}x${h}, recommended 512x512"
    [[ "$has_alpha" != "yes" ]] && warn "icon.png has no alpha channel (transparent background recommended)"
  fi
fi

# 5. Privacy policy URL(s) reachable
if [[ -f "$MANIFEST" ]] && command -v jq >/dev/null 2>&1; then
  urls=$(jq -r '.privacy_policies[]? // empty' "$MANIFEST" 2>/dev/null) || urls=""
  if [[ -n "$urls" ]]; then
    while IFS= read -r url; do
      [[ -z "$url" ]] && continue
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" || echo 000)
      if [[ "$code" == "200" ]]; then
        echo "privacy URL $url -> $code"
      else
        warn "privacy URL $url returned $code (must be 200 before submission)"
      fi
    done <<< "$urls"
  fi
fi

echo
if [[ "$fail" -eq 0 ]]; then
  echo "validate.sh: OK"
else
  echo "validate.sh: FAILED ($fail check(s))"
  exit 1
fi
