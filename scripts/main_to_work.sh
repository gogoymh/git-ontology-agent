#!/bin/sh
# Carries what reached main directly (external pull requests merged on GitHub) into the work branch right away, so the
# nightly 'Preconquer to main' run finds main already inside the work branch instead of clashing on it.
#
# Meant to run from the Preconquer workflow 'Main into preconquer' in the project folder, whatever branch that folder
# has checked out:
#
#   git show preconquer-gogoymh:scripts/main_to_work.sh | WORK_BRANCH=preconquer-gogoymh sh
#
# One run:
#   1. fetches main and the work branch from the remote. No git folder, no remote, or no main there: says why and
#      ends with status 0.
#   2. picks the base: the local work branch when it holds the remote one (missions merge into the local branch),
#      else the remote one. A local branch gone apart from the remote is left alone (status 0).
#   3. main already inside the base: nothing to do (status 0).
#   4. merges main into the base without touching any checkout (git merge-tree): a fast-forward when the base has
#      nothing of its own, else a merge commit naming the pull requests. A clash pushes nothing, names the files and
#      ends with status 0: the nightly run hands it to a mission.
#   5. moves the local work branch first (a fast-forward in the folder that has it checked out, else a checked
#      update-ref), so a mission merged meanwhile is never lost: if the branch moved, nothing is pushed and the next
#      run tries again. Then pushes the result to the remote work branch, never forced.
# Every line said also goes to the Preconquer log ($PRECONQUER_HOME, else ~/.preconquer, logs/preconquer.log).
# Status 1 only when git or the remote fails.
# Usage: [WORK_BRANCH=preconquer-gogoymh] [MAIN_BRANCH=main] sh main_to_work.sh [remote]
set -u
REMOTE="${1:-origin}"
WORK="${WORK_BRANCH:-preconquer-gogoymh}"
MAIN="${MAIN_BRANCH:-main}"
LOGS="${PRECONQUER_HOME:-$HOME/.preconquer}/logs"
say() {
    echo "[main-to-work] $*"
    mkdir -p "$LOGS" 2>/dev/null && echo "$(date '+%Y-%m-%d %H:%M:%S') $$ main-to-work: $*" >>"$LOGS/preconquer.log" 2>/dev/null
    return 0
}
fail() { say "FAILED: $*"; exit 1; }
idle() { say "nothing to do: $*"; exit 0; }
short() { git rev-parse --short "$1"; }
# git commands that talk to the remote give up on a dead link instead of hanging the routine.
if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -z "${GIT_SSH:-}" ] && [ -z "$(git config core.sshCommand 2>/dev/null)" ]; then
    export GIT_SSH_COMMAND="ssh -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
fi
export GIT_HTTP_LOW_SPEED_LIMIT="${GIT_HTTP_LOW_SPEED_LIMIT:-1000}" GIT_HTTP_LOW_SPEED_TIME="${GIT_HTTP_LOW_SPEED_TIME:-60}"

git rev-parse --git-dir >/dev/null 2>&1 || idle "$(pwd) is not a git folder"
git remote get-url "$REMOTE" >/dev/null 2>&1 || idle "no remote '$REMOTE' in $(pwd)"
heads=$(git ls-remote --heads "$REMOTE" "$MAIN" "$WORK" 2>&1) || fail "could not reach $REMOTE: $(echo "$heads" | head -n 1)"
echo "$heads" | grep -q "refs/heads/$MAIN\$" || idle "$REMOTE has no $MAIN branch"
echo "$heads" | grep -q "refs/heads/$WORK\$" || idle "$REMOTE has no $WORK branch"
out=$(git fetch -q "$REMOTE" "+refs/heads/$MAIN:refs/remotes/$REMOTE/$MAIN" "+refs/heads/$WORK:refs/remotes/$REMOTE/$WORK" 2>&1) ||
    fail "could not fetch $MAIN and $WORK from $REMOTE: $(echo "$out" | head -n 1)"
main=$(git rev-parse "refs/remotes/$REMOTE/$MAIN") || fail "no $REMOTE/$MAIN"
remote_work=$(git rev-parse "refs/remotes/$REMOTE/$WORK") || fail "no $REMOTE/$WORK"

# The base: the local work branch when it holds the remote one, so missions merged only locally stay in.
local_work=$(git rev-parse -q --verify "refs/heads/$WORK" || true)
base=$remote_work
if [ -n "$local_work" ] && [ "$local_work" != "$remote_work" ]; then
    if git merge-base --is-ancestor "$remote_work" "$local_work"; then
        base=$local_work
    elif ! git merge-base --is-ancestor "$local_work" "$remote_work"; then
        idle "local $WORK ($(short "$local_work")) and $REMOTE/$WORK ($(short "$remote_work")) have gone apart; left for the nightly run"
    fi
fi

git merge-base --is-ancestor "$main" "$base" && idle "$WORK ($(short "$base")) already holds $MAIN ($(short "$main"))"

count=$(git rev-list --count "$base..$main")
prs=$(git log --format=%s "$base..$main" | sed -n 's/^Merge pull request \(#[0-9][0-9]*\).*/\1/p' | sort -u | tr '\n' ' ' | sed 's/ $//')
say "$MAIN has $count commit(s) $WORK lacks${prs:+ (pull requests $prs)}"
if git merge-base --is-ancestor "$base" "$main"; then
    result=$main
    say "fast-forward $WORK $(short "$base") -> $(short "$main")"
else
    # Exit status 1 is a clash (conflicted files follow the tree line), above 1 an error.
    merged=$(git merge-tree --write-tree --name-only --no-messages "$base" "$main")
    status=$?
    if [ "$status" -eq 1 ]; then
        say "$MAIN clashes with $WORK in:"
        echo "$merged" | sed -n '2,$p' | sed '/^$/d; s/^/  /'
        idle "nothing pushed; the nightly 'Preconquer to main' run queues a mission to settle it"
    fi
    [ "$status" -eq 0 ] || fail "git merge-tree could not merge: $merged"
    tree=$(echo "$merged" | head -n 1)
    result=$(git commit-tree "$tree" -p "$base" -p "$main" \
        -m "Merge $MAIN into $WORK${prs:+: pull requests $prs}" -m "$count commit(s) that reached $MAIN directly.") ||
        fail "could not make the merge commit"
    say "merge commit $(short "$result") of $MAIN into $WORK"
fi

# Local branch first: a mission merged meanwhile makes this refuse, and nothing is pushed.
if [ -n "$local_work" ]; then
    folder=$(git worktree list --porcelain | awk -v ref="branch refs/heads/$WORK" '/^worktree /{w=substr($0,10)} $0==ref{print w; exit}')
    if [ -n "$folder" ]; then
        [ "$(git -C "$folder" rev-parse HEAD)" = "$local_work" ] || idle "local $WORK moved during the run; trying again next run"
        said=$(git -C "$folder" merge -q --ff-only "$result" 2>&1) ||
            idle "local $WORK in $folder left at $(short "$local_work"): $(echo "$said" | head -n 1); nothing pushed"
        say "fast-forwarded local $WORK in $folder to $(short "$result")"
    else
        git update-ref "refs/heads/$WORK" "$result" "$local_work" || idle "local $WORK moved during the run; nothing pushed"
        say "moved local $WORK to $(short "$result")"
    fi
fi
out=$(git push -q "$REMOTE" "$result:refs/heads/$WORK" 2>&1) ||
    fail "$REMOTE/$WORK was not pushed (it moved since the fetch?): $(echo "$out" | head -n 1)"
git fetch -q "$REMOTE" "+refs/heads/$WORK:refs/remotes/$REMOTE/$WORK" 2>/dev/null || true
say "pushed $REMOTE/$WORK at $(short "$result")"
