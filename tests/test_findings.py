"""Tests for the findings job's script, loaded from the workflow so the tested code is the shipped code."""
import json
import pathlib
import textwrap
import types
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pr-agent-review.yml"
HEAD = "a" * 40
OLD = "b" * 40
SUMMARY_URL = "https://github.com/o/r/pull/1#issuecomment-1"
NO_MAJOR = "<tr><td>⚡&nbsp;<strong>No major issues detected</strong></td></tr>"
NO_SECURITY = "<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>"


def load_script():
    lines = WORKFLOW.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == "- name: Fail on open findings")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    indent = None
    for line in lines[run + 1:]:
        if line.strip():
            current = len(line) - len(line.lstrip())
            if indent is None:
                indent = current
            elif current < indent:
                break
        body.append(line)
    module = types.ModuleType("findings")
    exec(compile(textwrap.dedent("\n".join(body)), str(WORKFLOW), "exec"), module.__dict__)
    return module


findings = load_script()


def summary(state=None, rows=(NO_MAJOR, NO_SECURITY), extra=""):
    parts = ["<!-- pr-agent:review:full -->", "## PR Reviewer Guide", *rows]
    if state is not None:
        parts += ["<!-- pr-agent-review-state:v1", json.dumps(state), "-->"]
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def finding(body, state="ACTIVE", path="src/a.py", line=3):
    return {"body": body, "state": state, "path": path, "line_start": line}


def thread(tid, body, resolved=False, path="src/a.py"):
    return {"id": tid, "resolved": resolved, "path": path, "url": f"https://example/{tid}", "body": body}


class FakeGitHub:
    def __init__(self, bodies, threads=(), fail_resolve=()):
        self.bodies = list(bodies)
        self.threads = list(threads)
        self.fail_resolve = set(fail_resolve)
        self.resolved = []

    def summary_comment(self, repo, pr):
        if not self.bodies:
            return None
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return {"body": body, "html_url": SUMMARY_URL}

    def bot_threads(self, repo, pr):
        return self.threads

    def resolve_thread(self, thread_id):
        if thread_id in self.fail_resolve:
            raise RuntimeError("Resource not accessible by integration")
        self.resolved.append(thread_id)


def run(github):
    output = []
    code = findings.main(github, "o/r", "1", HEAD, out=output.append, sleep=lambda _: None)
    return code, "\n".join(output)


def state_of(*items, head=HEAD, **last_run):
    return {"findings": list(items), "last_run": {"head_sha": head, **last_run}}


class SplitSummaryTests(unittest.TestCase):
    def test_state_block_is_removed_from_visible_text(self):
        state = state_of(finding(f"quotes {NO_SECURITY}"))
        parsed, visible = findings.split_summary(summary(state, rows=(NO_MAJOR,)))
        self.assertEqual(parsed, state)
        self.assertNotIn("pr-agent-review-state", visible)
        self.assertFalse(findings.has_verdict(visible, "🔒", "No security concerns identified"))

    def test_crlf_bodies_parse(self):
        parsed, visible = findings.split_summary(summary(state_of()).replace("\n", "\r\n"))
        self.assertEqual(parsed["last_run"]["head_sha"], HEAD)
        self.assertTrue(findings.has_verdict(visible, "⚡", "No major issues detected"))

    def test_without_state_the_footer_commit_is_used(self):
        body = summary(extra=f"Review updated until commit https://github.com/o/r/commit/{OLD})")
        parsed, _ = findings.split_summary(body)
        self.assertIsNone(parsed)
        self.assertEqual(findings.reviewed_sha(parsed, body), OLD)


class MainTests(unittest.TestCase):
    def test_clean_review_passes(self):
        code, output = run(FakeGitHub([summary(state_of())]))
        self.assertEqual(code, 0, output)
        self.assertIn("No open PR-Agent findings.", output)

    def test_missing_summary_fails(self):
        code, output = run(FakeGitHub([]))
        self.assertEqual(code, 1)
        self.assertIn("posted no review summary", output)

    def test_waits_for_summary_of_head_commit(self):
        github = FakeGitHub([summary(state_of(head=OLD)), summary(state_of())])
        self.assertEqual(run(github)[0], 0)

    def test_stale_summary_fails(self):
        code, output = run(FakeGitHub([summary(state_of(head=OLD))]))
        self.assertEqual(code, 1)
        self.assertIn(f"for commit {OLD[:7]}, not {HEAD[:7]}", output)

    def test_active_finding_with_open_thread_fails(self):
        github = FakeGitHub([summary(state_of(finding("Race in save")))], [thread("T1", "Race in save\n\ndetails")])
        code, output = run(github)
        self.assertEqual(code, 1)
        self.assertIn("- [src/a.py:3](https://example/T1) Race in save", output)

    def test_active_finding_with_resolved_threads_is_dismissed(self):
        github = FakeGitHub([summary(state_of(finding("Race in save")))], [thread("T1", "Race in save", resolved=True)])
        self.assertEqual(run(github)[0], 0)

    def test_resolving_the_thread_in_another_file_does_not_dismiss(self):
        github = FakeGitHub([summary(state_of(finding("Race in save")))],
                            [thread("T1", "Race in save", resolved=True, path="src/other.py")])
        code, output = run(github)
        self.assertEqual(code, 1)
        self.assertIn(f"({SUMMARY_URL}) Race in save (only in the summary", output)

    def test_security_concern_in_summary_fails(self):
        code, output = run(FakeGitHub([summary(state_of(), rows=(NO_MAJOR,))]))
        self.assertEqual(code, 1)
        self.assertIn("reports a security concern", output)

    def test_without_state_major_issues_fail(self):
        code, output = run(FakeGitHub([summary(rows=(NO_SECURITY,))]))
        self.assertEqual(code, 1)
        self.assertIn("has no finding state", output)

    def test_resolves_only_threads_of_fixed_findings(self):
        state = state_of(
            finding("Fixed one", state="RESOLVED"),
            finding("Raised again", state="RESOLVED"),
            finding("Raised again", state="ACTIVE"),
            finding("Fixed elsewhere", state="RESOLVED", path="src/b.py"),
        )
        threads = [
            thread("T_fixed", "Fixed one"),
            thread("T_reraised", "Raised again"),
            thread("T_already", "Fixed one", resolved=True),
            thread("T_other_file", "Fixed elsewhere", path="src/c.py"),
            thread("T_unknown", "Not in the state"),
        ]
        github = FakeGitHub([summary(state)], threads)
        code, _ = run(github)
        self.assertEqual(github.resolved, ["T_fixed"])
        self.assertEqual(code, 1)  # "Raised again" is still active.

    def test_failed_resolve_is_a_warning(self):
        github = FakeGitHub([summary(state_of(finding("Fixed", state="RESOLVED")))], [thread("T1", "Fixed")],
                            fail_resolve={"T1"})
        code, output = run(github)
        self.assertEqual(code, 0, output)
        self.assertIn("::warning::Could not resolve review thread T1", output)

    def test_incomplete_review_warns(self):
        state = state_of(complete=False, excluded_files=["big.cs"])
        code, output = run(FakeGitHub([summary(state)]))
        self.assertEqual(code, 0)
        self.assertIn("::warning::The review was incomplete. Files left out: big.cs", output)


if __name__ == "__main__":
    unittest.main()
