#!/usr/bin/env bash
# wave_tag.sh — durable, in-repo wave counter (#108).
#
# A "wave" is one parallel batch of roadmap stories integrated into main. Nothing
# in the repo recorded how many waves have run, so each orchestrating session
# numbered from 1 again and "every 3rd wave" was impossible to evaluate. This
# script tags the wave's merge point with an ANNOTATED tag `wave-NNN` (zero-padded,
# next after the highest existing) whose message lists the wave's stories/PRs.
# `git tag -l 'wave-*'` then counts waves from any session, offline, forever.
#
# FOR THE ORCHESTRATOR, at wave closeout. It creates the tag LOCALLY and prints
# the exact push command — it never pushes (workers never push; the orchestrator
# controls the remote). Usage:
#
#   scripts/wave_tag.sh 82 83 84        # tag next wave with stories #82 #83 #84
#   scripts/wave_tag.sh --count         # print how many wave-* tags exist
#   scripts/wave_tag.sh --next          # print the next wave number (no tag)
#
# Story args may be bare numbers or `#82` — both normalise to `#82`.
set -euo pipefail

PREFIX="wave-"
PAD=3

# Highest existing wave number (0 if none). Robust to gaps: uses max, not count.
highest_wave() {
	local max=0 n
	while IFS= read -r tag; do
		[ -n "$tag" ] || continue
		n="${tag#"$PREFIX"}"
		# ignore anything that isn't a plain number after the prefix
		case "$n" in
			''|*[!0-9]*) continue ;;
		esac
		n=$((10#$n))
		[ "$n" -gt "$max" ] && max="$n"
	done < <(git tag -l "${PREFIX}*")
	printf '%s\n' "$max"
}

# Count of existing wave-* tags (equals the highest number when contiguous).
count_waves() {
	git tag -l "${PREFIX}*" | grep -c . || true
}

case "${1:-}" in
	--count)
		count_waves
		exit 0
		;;
	--next)
		printf '%0*d\n' "$PAD" "$(( $(highest_wave) + 1 ))"
		exit 0
		;;
	-h|--help|"")
		grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '1d'
		[ -z "${1:-}" ] && exit 1 || exit 0
		;;
esac

# Remaining args are the wave's stories/PRs.
stories=()
for arg in "$@"; do
	s="${arg#\#}"
	case "$s" in
		''|*[!0-9]*)
			echo "wave_tag.sh: story arg '$arg' is not a number (expected 82 or #82)" >&2
			exit 2
			;;
	esac
	stories+=("#$s")
done

next=$(( $(highest_wave) + 1 ))
tag=$(printf '%s%0*d' "$PREFIX" "$PAD" "$next")

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
	echo "wave_tag.sh: $tag already exists — refusing to overwrite" >&2
	exit 3
fi

story_list=$(printf '%s, ' "${stories[@]}"); story_list="${story_list%, }"
message=$(printf 'Wave %d\n\nStories: %s\n' "$next" "$story_list")

git tag -a "$tag" -m "$message"

echo "created annotated tag $tag at $(git rev-parse --short HEAD)"
echo "  stories: $story_list"
echo
echo "push it (this script does NOT push):"
echo "  git push origin $tag"

# Every 3rd wave: nudge the drift suite (the skill closeout enforces this).
if [ $(( next % 3 )) -eq 0 ]; then
	echo
	echo "note: wave $next is a multiple of 3 — run 'make drift' and record findings (#108)."
fi
