#!/usr/bin/env bash
# Install the tracked-name leak sensor as a git pre-push hook (#104).
#
# The hook runs scripts/check_names.py against the range being pushed and its
# commit messages, blocking the push if a tracked company/client name (or a
# confirmed paraphrase) was added. Bypass an intentional collision with
# NAMES_CHECK_SKIP="reason" git push ...
#
# Worktree-aware: writes to the resolved hooks dir (git rev-parse --git-path),
# which is the shared common dir even from inside a linked worktree.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

HOOK_PATH="$(git -C "$REPO_ROOT" rev-parse --git-path hooks/pre-push)"
case "$HOOK_PATH" in
  /*) ;;                                   # already absolute
  *) HOOK_PATH="$REPO_ROOT/$HOOK_PATH" ;;  # relative to repo root
esac

if [ -e "$HOOK_PATH" ] && ! grep -q "check_names.py" "$HOOK_PATH" 2>/dev/null; then
  echo "A pre-push hook already exists and is not the names sensor:" >&2
  echo "  $HOOK_PATH" >&2
  echo "Refusing to overwrite. Merge the check in by hand, or remove it first." >&2
  exit 1
fi

mkdir -p "$(dirname "$HOOK_PATH")"
cat > "$HOOK_PATH" <<'HOOK'
#!/usr/bin/env bash
# Nebula tracked-name leak sensor (#104). Installed by scripts/install-hooks.sh.
# Bypass an intentional collision with NAMES_CHECK_SKIP="reason".
set -euo pipefail
TOPLEVEL="$(git rev-parse --show-toplevel)"
exec python3 "$TOPLEVEL/scripts/check_names.py" --pre-push
HOOK
chmod +x "$HOOK_PATH"

echo "Installed pre-push hook at:"
echo "  $HOOK_PATH"
