"""Tests for the checkpoint script, loaded from the workflow so the tested code is the shipped code."""
import json
import pathlib
import textwrap
import types
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pr-agent-review.yml"
A, B, C = "a" * 40, "b" * 40, "c" * 40


def load_script():
    lines = WORKFLOW.read_text(encoding="utf-8").split("\n")
    run = next(i for i, line in enumerate(lines) if line.strip() == "run: &checkpoint_script |")
    body, indent = [], None
    for line in lines[run + 1:]:
        if line.strip():
            current = len(line) - len(line.lstrip())
            if indent is None:
                indent = current
            elif current < indent:
                break
        body.append(line)
    module = types.ModuleType("checkpoint")
    exec(compile(textwrap.dedent("\n".join(body)), str(WORKFLOW), "exec"), module.__dict__)
    return module


cp = load_script()


def issue(path, header, content, line=10):
    return {"relevant_file": path, "issue_header": header, "issue_content": content, "start_line": line, "end_line": line}


def review(*issues, security="No"):
    return {"key_issues_to_review": list(issues), "security_concerns": security}


def thread(tid, path, body, resolved=False):
    return {"id": tid, "path": path, "body": body, "resolved": resolved, "url": f"https://example/{tid}"}


def posted(tid, path, header, content, resolved=False):
    """An inline thread as PR-Agent posts it, with its dedup marker."""
    marker = f"<!-- pr-agent-dedup: {cp.inline_fingerprint(path, header, content)} -->"
    return thread(tid, path, f"{cp.inline_body(header, content)}\n\n{marker}", resolved)


class FakeGitHub:
    """A pull request whose head, files and comments the tests control."""

    def __init__(self, head, files, compares=None, contents=None):
        self.heads = [head]
        self.files = [{"filename": f, "patch": f"@@ patch of {f}"} for f in files]
        self.compares = compares or {}
        self.contents = contents or {}
        self.issue_comments = []
        self.threads = []
        self.resolved = []
        self.saved = 0
        self.base_ref = "main"
        self.merge_base = "m" * 40

    def pull(self, repo, pr):
        head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        return {"head": {"sha": head}, "base": {"sha": "base", "ref": self.base_ref}}

    def pull_files(self, repo, pr):
        return self.files

    def compare(self, repo, base, head):
        if base == "base":  # The merge-base lookup against the pull request's base.
            return {"status": "ahead", "files": [], "merge_base_commit": {"sha": self.merge_base}}
        return self.compares.get((base, head))

    def file_at(self, repo, path, ref):
        return self.contents.get(path, f"contents of {path}")

    def comments(self, repo, pr):
        return [dict(c) for c in self.issue_comments]

    def save_comment(self, repo, pr, comment_id, body):
        if len(body) > 65_536:
            raise AssertionError(f"GitHub would reject a {len(body)}-character comment")
        self.saved += 1
        if comment_id is None:
            comment = {"id": len(self.issue_comments) + 1, "user": {"login": "github-actions[bot]"},
                       "body": body, "html_url": "https://example/checkpoint"}
            self.issue_comments.append(comment)
            return comment
        comment = next(c for c in self.issue_comments if c["id"] == comment_id)
        comment["body"] = body
        return comment

    def bot_threads(self, repo, pr):
        return self.threads

    def resolve_thread(self, thread_id):
        self.resolved.append(thread_id)

    def state(self):
        return cp.find_checkpoint(self.issue_comments)[1]


def run_review(github, head, result, verdicts=None, prompts=None, globs=()):
    calls = []

    def fake_review(plan_result, pr_paths, user_globs, model, keys):
        calls.append(plan_result)
        if isinstance(result, Exception):
            raise result
        return result

    def fake_ask(prompt):
        if prompts is not None:
            prompts.append(prompt)
        answer = {"verdicts": [{"id": i, "verdict": v, "reason": f"{v} because"} for i, v in (verdicts or {}).items()]}
        return answer, {"total_tokens": 100}

    output = []
    code = cp.review_main(github, "o/r", "1", head, "gpt-6.1-sol", "gpt-6.1-sol", "medium", {"openai": "k"},
                          list(globs), run_review=fake_review, ask=fake_ask, out=output.append)
    return code, calls, "\n".join(output)


def finding_ids(state, path, header):
    return [f["id"] for f in state["findings"] if f["path"] == path and f["header"] == header]


class WorkflowTests(unittest.TestCase):
    def test_pr_agent_accepts_edited_events(self):
        line = next(l for l in WORKFLOW.read_text().split("\n") if "GITHUB_ACTION_CONFIG.PR_ACTIONS:" in l)
        self.assertIn('"edited"', line)


class StorageTests(unittest.TestCase):
    def test_state_round_trips_even_with_comment_terminators_in_text(self):
        state = {"head_sha": A, "findings": [{"content": "closes the comment --> <b>"}], "runs": [], "security": {}}
        body = cp.render_comment({**state, "findings": []}).replace(cp.encode_state({**state, "findings": []}), cp.encode_state(state))
        self.assertNotIn("-->\n<b>", body)
        self.assertEqual(cp.parse_checkpoint(body), state)

    def test_glob_escape_matches_only_the_literal_path(self):
        import fnmatch
        glob = cp.glob_escape("src/[id]/page*.tsx")
        self.assertTrue(fnmatch.fnmatch("src/[id]/page*.tsx", glob))
        self.assertFalse(fnmatch.fnmatch("src/i/pageX.tsx", glob))


class PlanTests(unittest.TestCase):
    def compare(self, status, files):
        return lambda base, head: {"status": status, "files": [{"filename": f} for f in files]}

    def test_first_run_is_a_full_review(self):
        self.assertEqual(cp.plan(None, A, "main", "M", {"x.py"}, None, [])["mode"], "full")

    def test_checkpoint_at_head_needs_nothing(self):
        self.assertEqual(cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M"}, A, "main", "M", {"x.py"}, None, [])["mode"], "none")

    def test_rebased_branch_gets_a_full_review(self):
        self.assertEqual(cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M"}, B, "main", "M", {"x.py"}, self.compare("diverged", ["x.py"]), [])["mode"], "full")
        self.assertEqual(cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M"}, B, "main", "M", {"x.py"}, lambda b, h: None, [])["mode"], "full")

    def test_delta_covers_changed_pr_files_minus_ignored(self):
        result = cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M", "ignore_glob": ["**/package-lock.json"]},
                         B, "main", "M", {"x.py", "y.py", "package-lock.json"},
                         self.compare("ahead", ["x.py", "package-lock.json", "not-in-pr.py"]), ["**/package-lock.json"])
        self.assertEqual((result["mode"], result["files"]), ("delta", ["x.py"]))

    def test_push_touching_no_reviewable_file_reviews_nothing(self):
        result = cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M"}, B, "main", "M", {"x.py"}, self.compare("ahead", ["elsewhere.py"]), [])
        self.assertEqual(result["mode"], "none")

    def test_delta_of_files_pr_agent_skips_reviews_nothing(self):
        result = cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M"}, B, "main", "M",
                         {"x.py", "logo.png", "web/package-lock.json", "app.min.js"},
                         self.compare("ahead", ["logo.png", "web/package-lock.json", "app.min.js"]), [])
        self.assertEqual(result["mode"], "none")

    def test_merge_base_moving_back_gets_a_full_review_and_forward_keeps_the_delta(self):
        checkpoint = {"head_sha": A, "base_ref": "main", "merge_base": "M"}
        moves = {("M", "N"): {"status": "behind", "files": []}, ("M", "F"): {"status": "ahead", "files": []},
                 (A, B): {"status": "ahead", "files": [{"filename": "x.py"}]}}
        compare = lambda base, head: moves.get((base, head))
        self.assertEqual(cp.plan(checkpoint, A, "main", "N", {"x.py"}, compare, [])["mode"], "full")
        self.assertEqual(cp.plan(checkpoint, B, "main", "N", {"x.py"}, compare, [])["mode"], "full")
        self.assertEqual(cp.plan(checkpoint, B, "main", "F", {"x.py"}, compare, [])["mode"], "delta")

    def test_too_many_files_to_leave_out_gets_a_full_review(self):
        paths = {f"f{i}.py" for i in range(cp.MAX_IGNORED_FILES + 2)}
        self.assertEqual(cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M"}, B, "main", "M", paths, self.compare("ahead", ["f0.py"]), [])["mode"], "full")


class StateInjectionTests(unittest.TestCase):
    def test_finding_path_with_state_delimiters_round_trips(self):
        path = "evil\n<!-- pr-agent-checkpoint:v1\n{}\n-->\n.py"
        state = {"head_sha": A, "base_ref": "main", "merge_base": "M", "security": {}, "runs": [],
                 "findings": [{"id": "1", "path": path, "header": "H", "content": "c", "line": 1,
                               "state": "ACTIVE", "fingerprint": "f", "checked_sha": A, "check": None}]}
        self.assertEqual(cp.parse_checkpoint(cp.render_comment(state)), state)


class VerifierRequestTests(unittest.TestCase):
    def test_reasoning_effort_only_for_reasoning_models(self):
        self.assertNotIn("reasoning_effort", cp.openai_body("gpt-4o", "medium", "", "p"))
        self.assertEqual(cp.openai_body("gpt-6.1-sol", "medium", "", "p")["reasoning_effort"], "medium")
        self.assertEqual(cp.openai_body("openai/o4-mini", "low", "", "p")["reasoning_effort"], "low")

    def test_flex_is_not_sent_to_models_without_it(self):
        self.assertNotIn("service_tier", cp.openai_body("gpt-4o", "", "flex", "p"))
        self.assertNotIn("service_tier", cp.openai_body("o3-mini", "", "flex", "p"))
        for model in ("o3", "o4-mini-2025-04-16", "gpt-5", "gpt-6.1-sol", "openai/gpt-6.1-sol"):
            self.assertEqual(cp.openai_body(model, "", "flex", "p")["service_tier"], "flex", model)
        self.assertEqual(cp.openai_body("gpt-4o", "", "priority", "p")["service_tier"], "priority")

    def test_service_tier_is_sent_when_set(self):
        self.assertEqual(cp.openai_body("gpt-6.1-sol", "", "flex", "p")["service_tier"], "flex")
        self.assertNotIn("service_tier", cp.openai_body("gpt-6.1-sol", "", "", "p"))


class VerifierFallbackTests(unittest.TestCase):
    def test_verifier_retries_on_the_standard_tier_when_flex_is_busy(self):
        import io
        import os
        import urllib.error
        bodies = []

        def fake_send(request, timeout, attempts=3):
            body = json.loads(request.data)
            bodies.append(body)
            if "service_tier" in body:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {},
                                             io.BytesIO(b'{"error": "Flex does not have sufficient resources available"}'))
            answer = {"choices": [{"message": {"content": json.dumps({"verdicts": []})}}], "usage": {}}
            return json.dumps(answer).encode(), {}
        saved, cp.send = cp.send, fake_send
        os.environ["SERVICE_TIER"] = "flex"
        try:
            answer, _ = cp.call_model("gpt-6.1-sol", "medium", "prompt", {"openai": "k"})
        finally:
            cp.send = saved
        self.assertEqual(answer, {"verdicts": []})
        self.assertEqual(["service_tier" in b for b in bodies], [True, False])


class VerdictTests(unittest.TestCase):
    def test_missing_unknown_and_malformed_verdicts_are_uncertain(self):
        verdicts = cp.parse_verdicts({"verdicts": [{"id": "1", "verdict": "fixed", "reason": "r"},
                                                   {"id": "2", "verdict": "probably"}, "junk",
                                                   {"id": "x", "verdict": "fixed"}]}, ["1", "2", "3"])
        self.assertEqual(verdicts["1"][0], "fixed")
        self.assertEqual(verdicts["2"][0], "uncertain")
        self.assertEqual(verdicts["3"][0], "uncertain")


class ReviewTests(unittest.TestCase):
    def first_run(self):
        github = FakeGitHub(A, ["x.py", "y.py", "z.py"])
        code, calls, _ = run_review(github, A, review(issue("x.py", "Race", "Two saves interleave."),
                                                      issue("y.py", "Leak", "Handle never closed.")))
        self.assertEqual((code, calls[0]["mode"]), (0, "full"))
        return github

    def test_first_run_saves_findings_for_the_head(self):
        state = self.first_run().state()
        self.assertEqual(state["head_sha"], A)
        self.assertEqual(sorted(f["header"] for f in state["findings"]), ["Leak", "Race"])

    def test_delta_review_verifies_changed_files_and_advances_with_open_findings(self):
        github = self.first_run()
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "@@ fix"}]}
        race = finding_ids(github.state(), "x.py", "Race")[0]
        prompts = []
        code, calls, _ = run_review(github, B, review(issue("x.py", "Overflow", "Counter wraps.")),
                                    verdicts={race: "fixed"}, prompts=prompts)
        state = github.state()
        self.assertEqual((code, calls[0]["mode"], calls[0]["files"]), (0, "delta", ["x.py"]))
        self.assertEqual(state["head_sha"], B)
        by_header = {f["header"]: f for f in state["findings"]}
        self.assertEqual(by_header["Race"]["state"], "FIXED")
        self.assertEqual(by_header["Leak"]["state"], "ACTIVE")       # y.py did not change: not re-checked
        self.assertEqual(by_header["Overflow"]["state"], "ACTIVE")   # new blocking finding, still saved
        self.assertEqual(len(prompts), 1)
        self.assertIn("@@ fix", prompts[0])

    def test_unfixed_finding_stays_open(self):
        github = self.first_run()
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "@@ unrelated"}]}
        race = finding_ids(github.state(), "x.py", "Race")[0]
        run_review(github, B, review(), verdicts={race: "still_present"})
        self.assertEqual({f["header"]: f["state"] for f in github.state()["findings"]}["Race"], "ACTIVE")

    def test_finding_raised_again_word_for_word_is_still_present_without_a_verifier_call(self):
        github = self.first_run()
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        prompts = []
        run_review(github, B, review(issue("x.py", "Race", "Two saves interleave.", line=40)), prompts=prompts)
        race = {f["header"]: f for f in github.state()["findings"]}["Race"]
        self.assertEqual((race["state"], race["line"], prompts), ("ACTIVE", 40, []))

    def test_finding_without_a_line_is_tracked(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review({"relevant_file": "x.py", "issue_header": "Design", "issue_content": "Retries duplicate charges."}))
        finding = github.state()["findings"][0]
        self.assertEqual((finding["state"], finding["line"]), ("ACTIVE", None))

    def test_failed_review_saves_nothing_and_the_next_delta_still_covers_it(self):
        github = self.first_run()
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        with self.assertRaises(cp.RunFailed):
            run_review(github, B, cp.RunFailed("PR-Agent exited with code 1."))
        self.assertEqual(github.state()["head_sha"], A)
        github.heads = [C]
        github.compares[(A, C)] = {"status": "ahead", "files": [{"filename": "x.py"}, {"filename": "z.py"}]}
        _, calls, _ = run_review(github, C, review())
        self.assertEqual(calls[0]["files"], ["x.py", "z.py"])

    def test_head_moving_during_the_run_saves_nothing(self):
        github = self.first_run()
        saved = github.saved
        github.heads = [B, C]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "z.py"}]}
        with self.assertRaises(cp.RunFailed):
            run_review(github, B, review())
        self.assertEqual(github.saved, saved)

    def test_a_newer_checkpoint_is_not_overwritten(self):
        github = self.first_run()
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "z.py"}]}
        newer = cp.render_comment({"head_sha": C, "base_ref": "main", "merge_base": "m" * 40, "findings": [],
                                   "security": {}, "runs": []})
        calls = {"n": 0}
        original = github.comments

        def comments(repo, pr):
            # Another run saves its checkpoint between this run's first and second read.
            calls["n"] += 1
            if calls["n"] == 2:
                github.issue_comments[0]["body"] = newer
            return original(repo, pr)
        github.comments = comments
        with self.assertRaises(cp.RunFailed):
            run_review(github, B, review())
        self.assertEqual(github.state()["head_sha"], C)

    def test_rerun_on_the_saved_head_does_nothing(self):
        github = self.first_run()
        saved = github.saved
        code, calls, _ = run_review(github, A, review())
        self.assertEqual((code, calls, github.saved), (0, [], saved))

    def test_reverting_a_file_out_of_the_pr_still_verifies_its_findings(self):
        github = self.first_run()
        github.heads = [B]
        github.files = [f for f in github.files if f["filename"] != "x.py"]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "@@ revert"}]}
        race = finding_ids(github.state(), "x.py", "Race")[0]
        code, calls, _ = run_review(github, B, review(), verdicts={race: "fixed"})
        state = github.state()
        self.assertEqual((code, calls, state["head_sha"]), (0, [], B))
        self.assertEqual({f["header"]: f["state"] for f in state["findings"]}["Race"], "FIXED")

    def test_retargeted_base_at_the_same_head_gets_a_full_review(self):
        github = self.first_run()
        github.base_ref = "release"
        code, calls, _ = run_review(github, A, review())
        self.assertEqual((code, calls[0]["mode"]), (0, "full"))
        self.assertEqual(github.state()["base_ref"], "release")

    def test_checkpoint_stays_under_the_comment_limit_by_dropping_old_fixed_findings(self):
        # Twelve fix-and-review rounds of ten 1.5 KB findings each: far more history than fits.
        paths = [f"f{i}.py" for i in range(10)]
        github = FakeGitHub("0" * 40, paths)
        previous = None
        for round_number in range(12):
            head = f"{round_number:040d}"
            github.heads = [head]
            if previous:
                github.compares[(previous, head)] = {"status": "ahead", "files": [{"filename": p} for p in paths]}
            fixed = {f["id"]: "fixed" for f in (github.state() or {}).get("findings", []) if f["state"] == "ACTIVE"}
            issues = [issue(p, "Bug", f"round {round_number} " + "x" * 1500) for p in paths]
            code, _, _ = run_review(github, head, review(*issues), verdicts=fixed)
            self.assertEqual(code, 0)
            previous = head
        state = github.state()
        self.assertEqual(state["head_sha"], previous)
        self.assertGreater(state["pruned_fixed"], 0)
        self.assertEqual(sum(f["state"] == "ACTIVE" for f in state["findings"]), 10)

    def test_security_concern_forces_a_full_review_so_an_unrelated_delta_cannot_clear_it(self):
        github = FakeGitHub(A, ["x.py", "y.py"])
        run_review(github, A, review(security="Injection: x.py builds a shell command from input."))
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "y.py"}]}
        _, calls, _ = run_review(github, B, review(security="Injection: x.py builds a shell command from input."))
        self.assertEqual(calls[0]["mode"], "full")
        self.assertTrue(github.state()["security"]["concern"])

    def test_changing_the_ignore_globs_gets_a_full_review(self):
        github = FakeGitHub(A, ["x.py", "gen/client.py"])
        _, calls, _ = run_review(github, A, review(), globs=["gen/**"])
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        _, calls, _ = run_review(github, B, review(), globs=[])
        self.assertEqual(calls[0]["mode"], "full")
        self.assertEqual(github.state()["ignore_glob"], [])

    def test_incomplete_or_malformed_review_output_saves_nothing(self):
        for bad in ({"key_issues_to_review": []},
                    {"key_issues_to_review": [{"relevant_file": "x.py"}], "security_concerns": "No"},
                    {"key_issues_to_review": ["text"], "security_concerns": "No"},
                    {"key_issues_to_review": {}, "security_concerns": "No"}):
            github = FakeGitHub(A, ["x.py"])
            with self.assertRaises(cp.RunFailed, msg=bad):
                run_review(github, A, bad)
            self.assertEqual(github.saved, 0)

    def test_renamed_file_carries_its_findings_and_threads(self):
        github = self.first_run()
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.files = [f for f in github.files if f["filename"] != "x.py"] + [{"filename": "moved.py", "patch": "@@"}]
        github.contents["moved.py"] = "the same racy code"
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}]}
        prompts = []
        run_review(github, B, review(), verdicts={race: "still_present"}, prompts=prompts)
        self.assertEqual(len(prompts), 1)
        self.assertIn("File: moved.py (renamed from x.py", prompts[0])
        self.assertIn("the same racy code", prompts[0])
        moved = {f["header"]: f for f in github.state()["findings"]}["Race"]
        self.assertEqual((moved["path"], moved["thread_path"], moved["state"]), ("moved.py", "x.py", "ACTIVE"))
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]
        self.assertTrue(cp.thread_posts(github.threads[0], moved))

    def test_security_concern_is_recorded(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(security="SQL injection: the query concatenates input."))
        self.assertTrue(github.state()["security"]["concern"].startswith("SQL injection"))


class PrAgentProcessTests(unittest.TestCase):
    """Runs run_pr_agent against a stand-in PR-Agent that reports the environment it got."""

    def run_with(self, model, keys, plan_result, pr_paths, exit_code=0, write_review=True, flex_busy=False):
        import os
        import sys
        import tempfile
        probe = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        probe.write(textwrap.dedent(f"""
            import json, os
            seen = {{k: os.environ.get(k) for k in ("OPENAI_KEY", "ANTHROPIC_KEY", "ANTHROPIC.KEY", "IGNORE.GLOB", "LITELLM.EXTRA_BODY",
                                                     "PR_REVIEWER.PERSISTENT_FINDING_STATE", "PR_REVIEWER.PERSISTENT_COMMENT")}}
            if {flex_busy!r} and os.environ.get("LITELLM.EXTRA_BODY"):
                print("RateLimitError: Flex does not have sufficient resources available")
                raise SystemExit(1)
            if {write_review!r}:
                with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                    out.write("other=1\\n")
                    out.write("review=" + json.dumps({{"key_issues_to_review": [], "seen": seen}}) + "\\n")
            raise SystemExit({exit_code})
        """))
        probe.close()
        saved, cp.PR_AGENT = cp.PR_AGENT, [sys.executable, probe.name]
        os.environ.update({"OPENAI_KEY": "o-key", "ANTHROPIC_KEY": "a-key", "SERVICE_TIER": "flex"})
        try:
            return cp.run_pr_agent(plan_result, pr_paths, ["**/package-lock.json"], model, keys)
        finally:
            cp.PR_AGENT = saved
            os.unlink(probe.name)

    def test_delta_run_ignores_other_files_and_gets_only_its_providers_key(self):
        result = self.run_with("anthropic/claude-opus-5-5", {"anthropic": "a-key", "openai": "o-key"},
                               {"mode": "delta", "files": ["x.py"]}, {"x.py", "y[1].py"})
        seen = result["seen"]
        self.assertEqual((seen["ANTHROPIC.KEY"], seen["OPENAI_KEY"], seen["ANTHROPIC_KEY"]), ("a-key", None, None))
        self.assertIsNone(seen["LITELLM.EXTRA_BODY"])  # Flex is OpenAI-only.
        self.assertEqual(json.loads(seen["IGNORE.GLOB"]), ["**/package-lock.json", "y[[]1[]].py"])
        self.assertEqual((seen["PR_REVIEWER.PERSISTENT_FINDING_STATE"], seen["PR_REVIEWER.PERSISTENT_COMMENT"]),
                         ("false", "false"))

    def test_flex_is_not_sent_to_pr_agent_for_models_without_it(self):
        for model in ("gpt-4o", "o3-mini"):
            seen = self.run_with(model, {"anthropic": "", "openai": "o-key"},
                                 {"mode": "full", "files": ["x.py"]}, {"x.py"})["seen"]
            self.assertIsNone(seen["LITELLM.EXTRA_BODY"], model)

    def test_full_run_keeps_the_persistent_summary(self):
        seen = self.run_with("gpt-6.1-sol", {"anthropic": "", "openai": "o-key"},
                             {"mode": "full", "files": ["x.py"]}, {"x.py"})["seen"]
        self.assertEqual((seen["OPENAI_KEY"], seen["ANTHROPIC.KEY"], seen["PR_REVIEWER.PERSISTENT_COMMENT"]),
                         ("o-key", None, "true"))
        self.assertEqual(json.loads(seen["LITELLM.EXTRA_BODY"]), {"service_tier": "flex"})

    def test_flex_without_capacity_retries_on_the_standard_tier(self):
        seen = self.run_with("gpt-6.1-sol", {"anthropic": "", "openai": "o-key"},
                             {"mode": "full", "files": ["x.py"]}, {"x.py"}, flex_busy=True)["seen"]
        self.assertIsNone(seen["LITELLM.EXTRA_BODY"])

    def test_failure_or_missing_output_is_a_failed_run(self):
        with self.assertRaises(cp.RunFailed):
            self.run_with("gpt-6.1-sol", {"openai": "k"}, {"mode": "full", "files": []}, set(), exit_code=1)
        with self.assertRaises(cp.RunFailed):
            self.run_with("gpt-6.1-sol", {"openai": "k"}, {"mode": "full", "files": []}, set(), write_review=False)


class GateTests(unittest.TestCase):
    def gate(self, github, head=A):
        output = []
        code = cp.gate_main(github, "o/r", "1", head, out=output.append, sleep=lambda _: None)
        return code, "\n".join(output)

    def reviewed(self, *issues, security="No"):
        github = FakeGitHub(A, ["x.py", "y.py"])
        run_review(github, A, review(*issues, security=security))
        return github

    def test_no_checkpoint_for_head_fails(self):
        code, output = self.gate(self.reviewed(), head=B)
        self.assertEqual(code, 1)
        self.assertIn(f"No PR-Agent checkpoint for {B[:7]}", output)

    def test_clean_checkpoint_passes(self):
        self.assertEqual(self.gate(self.reviewed())[0], 0)

    def test_open_finding_fails_and_resolved_threads_dismiss_it(self):
        github = self.reviewed(issue("x.py", "Possible bug", "Two saves interleave."))
        github.threads = [posted("T1", "x.py", "Possible Issue", "Two saves interleave.")]
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("(https://example/T1) Possible Issue: Two saves interleave.", output)
        github.threads[0]["resolved"] = True
        self.assertEqual(self.gate(github)[0], 0)

    def test_fixed_findings_threads_are_resolved(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."), issue("y.py", "Leak", "Never closed."))
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        race = finding_ids(github.state(), "x.py", "Race")[0]
        run_review(github, B, review(), verdicts={race: "fixed"})
        github.threads = [posted("T_race", "x.py", "Race", "Two saves interleave."),
                          posted("T_leak", "y.py", "Leak", "Never closed.")]
        code, _ = self.gate(github, head=B)
        self.assertEqual((code, github.resolved), (1, ["T_race"]))

    def test_resolved_thread_of_one_finding_does_not_dismiss_a_similar_one(self):
        # The shared opening is longer than the stored content, so only the fingerprint tells them apart.
        shared = "Saves interleave when two requests " + "y" * cp.CONTENT_CHARS
        github = self.reviewed(issue("x.py", "Race", shared + " on create."), issue("x.py", "Race", shared + " on delete."))
        stored = github.state()["findings"]
        self.assertEqual(stored[0]["content"], stored[1]["content"])
        # Only the first was posted inline, and that thread was resolved; the second is summary-only.
        github.threads = [posted("T_old", "x.py", "Race", shared + " on create.", resolved=True)]
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("1 open PR-Agent finding", output)

    def test_finding_without_a_fingerprint_counts_as_summary_only(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        comment = github.issue_comments[0]
        state = github.state()
        del state["findings"][0]["fingerprint"]
        comment["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        self.assertEqual(self.gate(github)[0], 1)

    def test_threads_of_findings_pruned_for_size_are_still_resolved(self):
        paths = [f"f{i}.py" for i in range(10)]
        github = FakeGitHub("0" * 40, paths)
        previous, posted_threads = None, []
        for round_number in range(12):
            head = f"{round_number:040d}"
            github.heads = [head]
            if previous:
                github.compares[(previous, head)] = {"status": "ahead", "files": [{"filename": p} for p in paths]}
            fixed = {f["id"]: "fixed" for f in (github.state() or {}).get("findings", []) if f["state"] == "ACTIVE"}
            contents = [(p, f"round {round_number} " + "x" * 1500) for p in paths]
            run_review(github, head, review(*[issue(p, "Bug", c) for p, c in contents]), verdicts=fixed)
            posted_threads += [posted(f"T{round_number}-{p}", p, "Bug", c) for p, c in contents]
            github.threads = posted_threads
            previous = head
        self.assertGreater(github.state()["pruned_fixed"], 0)
        github.threads = posted_threads
        # Open threads keep every pruned identity, however many there are.
        self.assertEqual(len(github.state()["pruned"]), github.state()["pruned_fixed"])
        self.gate(github, head=previous)
        # Every thread but the ten of the last round, which are still open findings, is resolved.
        self.assertEqual(len(github.resolved), 110)
        self.assertFalse(any(t.startswith("T11-") for t in github.resolved))

    def test_pruned_identity_is_dropped_once_its_threads_are_resolved(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        comment = github.issue_comments[0]
        state = github.state()
        state["pruned"] = [{"path": "x.py", "fingerprint": "aaaaaaaaaaaa"}, {"path": "y.py", "fingerprint": "bbbbbbbbbbbb"}]
        comment["body"] = cp.render_comment(state)
        github.threads = [thread("T1", "x.py", "<!-- pr-agent-dedup: aaaaaaaaaaaa -->"),
                          thread("T2", "y.py", "<!-- pr-agent-dedup: bbbbbbbbbbbb -->", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "z.py"}]}
        run_review(github, B, review())
        self.assertEqual([p["fingerprint"] for p in github.state()["pruned"]], ["aaaaaaaaaaaa"])

    def test_security_concern_blocks(self):
        code, output = self.gate(self.reviewed(security="Secrets in logs: the token is printed."))
        self.assertEqual(code, 1)
        self.assertIn("Security concern", output)


if __name__ == "__main__":
    unittest.main()
