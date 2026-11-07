# PR-Agent review

A reusable GitHub Actions workflow that reviews pull requests with
[PR-Agent](https://github.com/qodo-ai/pr-agent) on your own Anthropic or OpenAI API key, and
turns its findings into a failing check.

It has two jobs:

- **review** runs PR-Agent 0.49.0 on every non-draft pull request from the same repository. It
  posts findings as inline threads and keeps one persistent summary comment that records each
  finding's state across reviews. The job fails if the review does not complete.
- **findings** reads that state back. It fails while a finding is still active and its inline
  thread is open, and while the summary reports a security concern. It also resolves the threads
  of findings a later review marked fixed, which PR-Agent does not do on GitHub.

To dismiss a finding you disagree with, resolve its thread and re-run the failed **findings** job.
That costs no API credits; only a new push triggers another review.

Neither job checks out or runs pull request code. PR-Agent reads the diff through the API in
restricted mode, and the findings job only reads comment text.

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

Do not add an `issue_comment` trigger. PR-Agent does not check who wrote a slash command, so on
a public repository any commenter could spend your API credits.

## Inputs

| Input | Default | |
| --- | --- | --- |
| `instructions` | required | What the repository is and what to focus on. Shared rules are appended: report only defects tied to a concrete failure, no style comments, no standalone missing-test findings. |
| `model` | `anthropic/claude-opus-5-5` | Any `anthropic/` or OpenAI model PR-Agent 0.49.0 supports, such as `gpt-6.1-sol`. There are no fallback models, so a failing model fails the check. |
| `reasoning-effort` | `high` | OpenAI reasoning effort, or the adaptive-thinking effort for Claude 5 models. |
| `ignore-glob` | `[]` | JSON list of globs to leave out, such as lockfiles, generated code and evidence records. PR-Agent already skips binary, image, font, archive and `.lock` files. |
| `runs-on` | `ubuntu-latest` | Runner label. Both jobs run in the pinned PR-Agent image, so runners that require a job container work too. |

Secrets: `ANTHROPIC_KEY` for `anthropic/` models, `OPENAI_KEY` for OpenAI models. Only the key
for the selected model reaches PR-Agent, and the review fails if it is missing.

Reviews are sized for completeness rather than cost: up to 200K input tokens, 64K output tokens,
large pull requests split into up to five calls, and at most ten findings. The run's cost is in
the summary comment.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests load the findings script from the workflow file itself, so they test the code that
runs.
