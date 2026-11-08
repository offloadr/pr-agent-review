# Checkpoint gate

Large pull requests reviewed on every push cost a full review each time: lumibelle#37 (100
files, +7K lines) took 15 full reviews at about $0.55 each in one day. The checkpoint gate keeps
a review on every push and a gate that applies to the current commit, but stops re-reading the
whole pull request after each fix.

## Invariant

Every push is reviewed, every earlier finding is accounted for, and the gate's verdict applies to
the current head commit.

## State

The workflow, not PR-Agent, owns the finding state. It lives in one comment that the workflow
token writes, in a hidden block:

```
<!-- pr-agent-checkpoint:v1
{"head_sha": ..., "base_sha": ..., "findings": [...], "security": {...}, "runs": [...]}
-->
```

PR-Agent's own persistent finding state is turned off, so there is one source of truth.

Each finding has an id, path, line, header, content, `state` (`ACTIVE` or `FIXED`), the commit
it was first seen on, and the latest verification: verdict, reason, commit and model. Fixed
findings are kept with their evidence rather than deleted.

## Each run of the review job

1. **Plan.** Read the pull request (head, base, files) and the checkpoint.
   - No checkpoint, an open security concern, changed ignore globs, a changed base branch, a merge base that moved back or sideways (base reset
     or rewritten, which can expose files no review has seen), a checkpoint head that is not an
     ancestor of the current head (rebase or force-push), or more changed files than the ignore
     list can hold: **full review**. A merge base that moved forward (base merged in) keeps the
     earlier review valid.
   - Otherwise: **delta review** of the PR files that changed between the checkpoint head and the
     current head, from GitHub's compare API. Range selection is exact; PR-Agent's own
     `/review -i` is not used because it picks commits by committer date against the previous
     review comment's creation time, which skips a commit made during a review and pushed after.
   - Delta files are filtered the way PR-Agent filters them (binary and lockfile extensions,
     generated lockfile names, minified and map files, the caller's ignore globs). A push that
     touches only such files has nothing to review, but earlier findings in the changed files are
     still verified.
   - Checkpoint already at the current head, base branch and merge base: nothing to review; the
     job succeeds.
2. **Review.** Run PR-Agent's action entrypoint. A delta review ignores every PR file outside the
   delta, so PR-Agent reviews the full pull-request diff of the changed files, with their
   surrounding PR changes as context. Findings are read from PR-Agent's `review` output (the parsed
   review, written to a private `GITHUB_OUTPUT` file), not from comments, so findings without an
   inline location are kept too. Inline threads are presentation only.
3. **Verify.** Every active finding that the review did not raise again, in a file that changed
   since it was last checked, goes to the verifier model. Findings are batched per file: one call
   gets the findings, the file at head and its diff since the finding was last checked, and must
   return `fixed`, `still_present` or `uncertain` per finding, with a reason. Only `fixed` closes a
   finding. A missing, malformed or `uncertain` verdict keeps it active, and keeps its range: the
   next verification still sees every change since the last definite verdict. Active findings in files the push did
   not change go to the verifier too, all in one call with the push's diff and a window of each
   finding's file, because a change elsewhere (a helper that stops returning None) can fix them.
   For a `fixed` verdict the verifier names the other files the fix relies on; a later push changing
   any of them (even reverting one out of the pull request) rechecks the fix, and a `still_present`
   or inconclusive answer reopens the finding. A verifier error fails the
   run.
4. **Save.** Re-read the pull request and the checkpoint. If the head moved or another run saved a
   newer checkpoint, fail without saving. Otherwise write the new checkpoint for the reviewed head.

Review jobs for one pull request run one at a time (a job-level concurrency group), and each
save is read back to confirm it stuck. A completed review advances the checkpoint even when findings remain: reviewing a commit is not
approving it. A failed, cancelled or interrupted run never advances it, so the next run's delta
covers everything since the last saved checkpoint.

## The findings job (the gate)

Reads the checkpoint and fails unless it is for the current head. An active finding blocks until
it is verified fixed or every inline thread posting it is resolved (a reviewer dismissing it). A
new finding, or one raised again after it was fixed (even after its record was pruned), is dismissed only by
resolving a thread opened since it came back, or by a resolved thread whose latest comment, written
since then by an owner, member or collaborator, starts with `/dismiss`. GitHub does not report when a
thread was resolved, so an old resolution or a plain later reply is no evidence. A finding keeps every thread identity it was
posted under, so a rename adds the new location instead of replacing the old one.
The findings job resolves only threads that existed when its checkpoint was saved, so a gate left
over from an older run cannot resolve, and so dismiss, a thread a newer review posted. It is not
in the review job's concurrency group, because GitHub keeps only one pending job per group and a
rerun findings job could replace a pending review.
The review job stores a dismissal as a `DISMISSED` state, so dismissed findings can be pruned like
fixed ones (their ids are remembered, so a dismissal outlives pruning and a review raising the
finding again keeps it dismissed). Unresolving a dismissed finding's thread makes it open again.
Threads are tied to findings by the `pr-agent-dedup` fingerprint in the footer PR-Agent appends, computed from the full
finding text when it is first read, so truncating stored text cannot make two findings share
threads. A
security concern from the latest review blocks until a later review reports none. Threads of fixed
findings are resolved with the workflow token, unless the thread also matches an active finding.
Re-running only this job re-evaluates dismissals without paying for a review.

## Known limits

- PR-Agent's `review` output has no coverage data, so the files a review left out or clipped for its
  token budget are read from the coverage footer of its summary comment and reviewed again on later
  runs until one covers them. The footer lists at most 50 files; beyond that the checkpoint records
  that more were left out, and the next push gets a full review to name them. Only pull requests
  larger than PR-Agent's call budget get there, and for those every push is a full review again.
- The history of pruned findings (fixed and dismissed ids, hashed thread identities of dismissed
  ones) is kept within about 20,000 characters, oldest dropped first. On a very long-lived pull
  request, unresolving the thread of a long-pruned dismissal may then go unnoticed; once trimmed,
  the gate fails closed on any unresolved PR-Agent thread it cannot place. Fixes that rely on other
  files keep a compact recheck record that is never trimmed: if those cannot fit, the save fails
  rather than forget a fix a revert could undo. Fixes in a finding's own file keep a smaller record
  (rechecked if that file is reverted out of the pull request) that counts toward the history budget,
  so on a very long-lived pull request the oldest of those can be trimmed.
- GitHub documents a 21,000-character limit for a step's `run` value. The shared script is about
  64,000 characters and runs today; moving it into a pinned script or action would remove that
  dependency on undocumented behavior.
- A delta review sees no earlier findings, so it may raise a still-present issue again in different
  words. That becomes a second finding; both close when verified fixed.
- PR-Agent's security verdict is not tied to files, so while a concern is open every run is a
  full review: only a review of the whole pull request that reports no concern clears it.
- The verifier reads pull-request content, so a crafted change could talk it into a `fixed`
  verdict. That gives a same-repository author nothing new: they can already dismiss a finding by
  resolving its thread.

## Pilot acceptance

- A delta review that raises a new blocking finding still advances the checkpoint, and the gate
  is red.
- A finding with no inline location is tracked and verified.
- A run cancelled after PR-Agent posted output does not advance the checkpoint; the next run's
  delta still covers its commits.
- A deliberately unfixed finding is not closed by the verifier.
- Per-push cost (delta review plus verification) is measured separately and compared with a full
  review of the same pull request.
