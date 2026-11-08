# PR-Agent review

A reusable GitHub Actions workflow that reviews pull requests with
[PR-Agent](https://github.com/qodo-ai/pr-agent) on your own Anthropic or OpenAI API key, and
turns its findings into a failing check.

Every push is reviewed, but only the first review reads the whole pull request:

- **review** runs PR-Agent 0.49.0 on every non-draft pull request from the same repository. The
  first run reviews the whole pull request. Later runs review only the files that changed since
  the last checkpoint, and a verifier model re-checks earlier findings in those files: it must
  name the change that fixed a finding before the finding closes. The job saves the result as a
  checkpoint comment for the reviewed commit, even when findings remain open. It fails, saving
  nothing, if the review or verification does not complete, so the next run's delta still
  covers those commits. A rebase or force-push gets a full review again.
- **findings** reads the checkpoint back. It fails unless the checkpoint is for the current
  commit, while any finding is open, and while the latest review reports a security concern. It
  also resolves the threads of findings the verifier marked fixed, which PR-Agent does not do on
  GitHub.

To dismiss a finding you disagree with, resolve its thread and re-run the failed **findings** job. A
finding that comes back after it was fixed is dismissed by resolving a thread opened for the new
occurrence, or by replying `/dismiss` on its old thread (as an owner, member or collaborator)
and resolving it: the earlier occurrence's resolution does not count.
That costs no API credits; only a new push triggers another review. The design and its limits are
in [docs/checkpoint-gate.md](docs/checkpoint-gate.md).

Neither job checks out or runs pull request code. PR-Agent reads the diff through the API in
restricted mode, and the scripts only read and write pull request data through the API.

## Use it

Pin the workflow by commit SHA:

```yaml
name: PR-Agent review

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]

concurrency:
  group: pr-agent-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  pr-agent:
    uses: offloadr/pr-agent-review/.github/workflows/pr-agent-review.yml@<commit-sha>
    permissions:
      contents: write # the findings job resolves threads, which GitHub gates on contents: write
      issues: write
      pull-requests: write
    secrets:
      ANTHROPIC_KEY: ${{ secrets.ANTHROPIC_KEY }}
    with:
      instructions: |
        Example is a .NET service that ... Focus on ...
      ignore-glob: '["package-lock.json", "src/Generated/**"]'
```

Adding `edited` to the trigger types reviews a retargeted pull request right away; without it, a
retarget is reviewed in full on the next push. Other edits run no review.

Do not add an `issue_comment` trigger. PR-Agent does not check who wrote a slash command, so on
a public repository any commenter could spend your API credits.

## Inputs

| Input | Default | |
| --- | --- | --- |
| `instructions` | required | What the repository is and what to focus on. Shared rules are appended: report only defects tied to a concrete failure, no style comments, no standalone missing-test findings. |
| `model` | `anthropic/claude-opus-5-5` | Any `anthropic/` or OpenAI model PR-Agent 0.49.0 supports, such as `gpt-6.1-sol`. There are no fallback models, so a failing model fails the check. |
| `reasoning-effort` | `high` | OpenAI reasoning effort, or the adaptive-thinking effort for Claude 5 models. |
| `service-tier` | `flex` | OpenAI service tier for the review and the verifier. Flex costs about half and is slower; when OpenAI has no Flex capacity, the review or verification waits and retries Flex three times (after 30 s, 1 min and 2 min), then runs once on the standard tier. A chunked review that Flex refuses partway through fails instead, because its finished chunks were already billed. Flex is sent only to models that offer it (o3, o4-mini, GPT-5 and later). Empty uses the standard tier; Anthropic models ignore it. |
| `verifier-model` | the review model | Model that re-checks earlier findings. A cheaper model lowers the cost of fix-and-review rounds; it only ever closes a finding with a `fixed` verdict. Each verifier call is capped at 16,000 output tokens, reasoning included. |
| `verifier-reasoning-effort` | `medium` | Reasoning effort for an OpenAI verifier. |
| `ignore-glob` | `[]` | JSON list of globs to leave out, such as lockfiles, generated code and evidence records. PR-Agent already skips binary, image, font, archive and `.lock` files. |
| `runs-on` | `ubuntu-latest` | Runner label. Both jobs run in the pinned PR-Agent image, so runners that require a job container work too. |

Secrets: `ANTHROPIC_KEY` for `anthropic/` models, `OPENAI_KEY` for OpenAI models, for both the
review and the verifier model. Only the review model's key reaches PR-Agent, and the review fails
if a needed key is missing.

Reviews are sized for completeness rather than cost: up to 200K input tokens, 64K output tokens,
large pull requests split into up to five calls, and at most ten findings. The run's cost is in
the summary comment.

## Development

```bash
python3 -m unittest discover -s tests
```

The review and findings jobs share one script, written once in the workflow and reused through a
YAML anchor. The tests load it from the workflow file itself, so they test the code that runs.
