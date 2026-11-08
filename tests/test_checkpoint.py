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
cp.FLEX_BACKOFF = (0, 0, 0)  # No real waiting in tests.


def issue(path, header, content, line=10):
    return {"relevant_file": path, "issue_header": header, "issue_content": content, "start_line": line, "end_line": line}


def review(*issues, security="No"):
    return {"key_issues_to_review": list(issues), "security_concerns": security}


def thread(tid, path, body, resolved=False, last_comment_at="2000-01-01T00:00:00Z"):
    return {"id": tid, "path": path, "body": body, "resolved": resolved, "url": f"https://example/{tid}",
            "last_comment_at": last_comment_at}


def current(t):
    """Mark a thread as posted for the finding's current occurrence (after the previous save)."""
    t["created_at"] = "9999-01-01T00:00:00Z"
    return t


def posted(tid, path, header, content, resolved=False, last_comment_at="2000-01-01T00:00:00Z"):
    """An inline thread as PR-Agent posts it, with its dedup marker."""
    marker = f"<!-- pr-agent-dedup: {cp.inline_fingerprint(path, header, content)} -->"
    return thread(tid, path, f"{cp.inline_body(header, content)}\n\n{marker}", resolved, last_comment_at)


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
        self.base_sha = "base"
        self.merge_base = "m" * 40

    def pull(self, repo, pr):
        head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        return {"head": {"sha": head}, "base": {"sha": self.base_sha, "ref": self.base_ref}}

    def pull_files(self, repo, pr):
        return self.files

    def compare(self, repo, base, head):
        if base == "base":  # The merge-base lookup against the pull request's base.
            return {"status": "ahead", "files": [], "merge_base_commit": {"sha": self.merge_base}}
        return self.compares.get((base, head))

    def is_binary(self, repo, path, ref):
        if (path, ref) in getattr(self, "binary_at", {}):
            return self.binary_at[(path, ref)]
        return path in getattr(self, "binary", set())

    def file_at(self, repo, path, ref):
        if (path, ref) in self.contents:
            return self.contents[(path, ref)]
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
        for t in self.threads:
            if t["id"] == thread_id:
                t["resolved"] = True

    def state(self):
        return cp.find_checkpoint(self.issue_comments)[1]


def run_review(github, head, result, verdicts=None, prompts=None, globs=(), relies=None):
    calls = []

    def fake_review(plan_result, pr_paths, user_globs, model, keys):
        calls.append(plan_result)
        if isinstance(result, Exception):
            raise result
        # PR-Agent posts or updates its summary comment on every review.
        github.issue_comments.append({"id": len(github.issue_comments) + 1, "user": {"login": "github-actions[bot]"},
                                      "body": "## PR Reviewer Guide\n<!-- pr-agent:review:full -->",
                                      "updated_at": cp.now_utc(), "html_url": "https://example/summary"})
        return result

    def fake_ask(prompt):
        if prompts is not None:
            prompts.append(prompt)
        answer = {"verdicts": [{"id": i, "verdict": v, "reason": f"{v} because", "relies_on": (relies or {}).get(i, [])}
                               for i, v in (verdicts or {}).items()]}
        return answer, {"total_tokens": 100}

    output = []
    code = cp.review_main(github, "o/r", "1", head, "gpt-6.1-sol", "gpt-6.1-sol", "medium", {"openai": "k"},
                          list(globs), run_review=fake_review, ask=fake_ask, out=output.append,
                          sleep=lambda _: None)
    return code, calls, "\n".join(output)


def checkpoint_comment(github):
    return next(c for c in github.issue_comments if c["body"].startswith(cp.CHECKPOINT_MARKER))


def finding_ids(state, path, header):
    return [f["id"] for f in state["findings"] if f["path"] == path and f["header"] == header]


class WorkflowTests(unittest.TestCase):
    def test_pr_agent_accepts_edited_events(self):
        line = next(l for l in WORKFLOW.read_text().split("\n") if "GITHUB_ACTION_CONFIG.PR_ACTIONS:" in l)
        self.assertIn('"edited"', line)


class CoverageTests(unittest.TestCase):
    def summary(self, body, updated_at="2026-11-07T12:00:00Z"):
        return {"user": {"login": "github-actions[bot]"}, "updated_at": updated_at,
                "body": "## PR Reviewer Guide\n<!-- pr-agent:review:full -->\n" + body}

    def test_coverage_footer_lists_files_left_out(self):
        body = ("<hr>\n\n⚠️ **Review coverage:** The following files were not included in this review because of "
                "the token budget:\n- `src/big.cs`\n- `src/other.cs`\n... and 3 more\n\n<hr>\nhelp")
        self.assertEqual(cp.coverage_gaps([self.summary(body)], "2026-11-07T11:00:00Z"),
                         (["src/big.cs", "src/other.cs"], True))

    def test_code_spans_decode_exactly(self):
        self.assertEqual(cp.decode_code_span("`src/a.py`"), "src/a.py")
        self.assertEqual(cp.decode_code_span("` src/file.py  `"), "src/file.py ")   # name ends with a space
        self.assertEqual(cp.decode_code_span("``a`b.py``"), "a`b.py")
        self.assertEqual(cp.decode_code_span("`` `odd`.py ``"), "`odd`.py")

    def test_entry_matching_two_pr_files_schedules_both(self):
        literal, newline = "src/a\\note.py", "src/a\note.py"
        body = ("⚠️ **Review coverage:** The following files were not included:\n- `src/a\\note.py`")
        paths, _ = cp.coverage_gaps([self.summary(body)], "2026-11-07T11:00:00Z", {literal, newline, "x.py"})
        self.assertEqual(sorted(paths), sorted([literal, newline]))

    def test_summaries_from_earlier_runs_are_ignored(self):
        body = "⚠️ **Review coverage:** The following files were not included:\n- `old.cs`"
        self.assertEqual(cp.coverage_gaps([self.summary(body, "2026-11-01T00:00:00Z")], "2026-11-07T11:00:00Z"),
                         ([], False))


class WorkflowConcurrencyTests(unittest.TestCase):
    def test_review_jobs_are_serialized_and_findings_jobs_cannot_evict_them(self):
        text = WORKFLOW.read_text()
        group = "group: pr-agent-checkpoint-${{ github.repository }}-${{ github.event.pull_request.number }}"
        review_job = text[text.index("  review:"):text.index("  findings:")]
        findings_job = text[text.index("  findings:"):]
        self.assertIn(group, review_job)
        self.assertIn("cancel-in-progress: false", review_job)
        # GitHub keeps one pending job per group, so a rerun findings job in it could replace a pending review.
        self.assertNotIn("concurrency:", findings_job)


class StorageTests(unittest.TestCase):
    def test_state_round_trips_even_with_comment_terminators_in_text(self):
        state = {"head_sha": A, "findings": [{"content": "closes the comment --> <b>"}], "runs": [], "security": {}}
        body = cp.render_comment({**state, "findings": []}).replace(cp.encode_state({**state, "findings": []}), cp.encode_state(state))
        self.assertNotIn("-->\n<b>", body)
        self.assertEqual(cp.parse_checkpoint(body), state)

    def test_a_comment_quoting_the_marker_is_not_the_checkpoint(self):
        real = {"id": 1, "user": {"login": "github-actions[bot]"},
                "body": cp.render_comment({"head_sha": A, "findings": [], "security": {}, "runs": []})}
        quoting = {"id": 2, "user": {"login": "github-actions[bot]"},
                   "body": "## PR Reviewer Guide\nThe gate looks for `<!-- pr-agent:checkpoint -->` in comments."}
        comment, state = cp.find_checkpoint([real, quoting])
        self.assertEqual((comment["id"], state["head_sha"]), (1, A))

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

    def test_ignore_list_too_long_for_the_environment_gets_a_full_review(self):
        long_paths = {f"{'deep/' * 140}file{i}.py" for i in range(150)}  # ~700 characters each, under the count cap
        paths = long_paths | {"x.py"}
        result = cp.plan({"head_sha": A, "base_ref": "main", "merge_base": "M"}, B, "main", "M", paths,
                         self.compare("ahead", ["x.py"]), [])
        self.assertEqual(result["mode"], "full")

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

    def test_pro_models_get_no_flex(self):
        self.assertFalse(cp.supports_flex("gpt-5-pro"))
        self.assertFalse(cp.supports_flex("openai/gpt-6.1-sol-pro"))
        self.assertTrue(cp.supports_flex("gpt-6.1-sol"))

    def test_a_rejected_service_tier_is_retried_on_the_standard_tier(self):
        import io
        import os
        import urllib.error
        bodies = []

        def fake_send(request, timeout, attempts=3):
            body = json.loads(request.data)
            bodies.append(body)
            if "service_tier" in body:
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(
                    b"{\"error\": \"Invalid value for 'service_tier': flex is not supported for this model.\"}"))
            return json.dumps({"choices": [{"message": {"content": '{"verdicts": []}'}}]}).encode(), {}
        saved, cp.send = cp.send, fake_send
        os.environ["SERVICE_TIER"] = "flex"
        try:
            cp.call_model("gpt-6.1-sol", "medium", "prompt", {"openai": "k"})
        finally:
            cp.send = saved
        self.assertEqual(["service_tier" in b for b in bodies], [True, False])

    def test_chat_variants_get_neither_reasoning_effort_nor_flex(self):
        body = cp.openai_body("gpt-5-chat-latest", "medium", "flex", "p")
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("service_tier", body)

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
        # Flex is retried with backoff before the standard tier.
        self.assertEqual(["service_tier" in b for b in bodies], [True] * (1 + len(cp.FLEX_BACKOFF)) + [False])

    def test_flex_that_frees_up_on_a_retry_stays_on_flex(self):
        import io
        import os
        import urllib.error
        bodies = []

        def fake_send(request, timeout, attempts=3):
            bodies.append(json.loads(request.data))
            if len(bodies) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {},
                                             io.BytesIO(b'{"error": "Flex does not have sufficient resources available"}'))
            return json.dumps({"choices": [{"message": {"content": '{"verdicts": []}'}}]}).encode(), {}
        saved, cp.send = cp.send, fake_send
        os.environ["SERVICE_TIER"] = "flex"
        try:
            cp.call_model("gpt-6.1-sol", "medium", "prompt", {"openai": "k"})
        finally:
            cp.send = saved
        self.assertEqual(["service_tier" in b for b in bodies], [True, True])


class ResponsesFlexTests(unittest.TestCase):
    def test_responses_api_retries_on_the_standard_tier_when_flex_is_busy(self):
        import io
        import urllib.error
        bodies = []

        def fake_send(request, timeout, attempts=3):
            body = json.loads(request.data)
            bodies.append(body)
            if "service_tier" in body:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {},
                                             io.BytesIO(b'{"error": "Flex does not have sufficient resources available"}'))
            answer = {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"verdicts": []}'}]}]}
            return json.dumps(answer).encode(), {}
        saved, cp.send = cp.send, fake_send
        try:
            answer, _ = cp.call_responses("gpt-6.1-sol", "high", "flex", "prompt", {"openai": "k"})
        finally:
            cp.send = saved
        self.assertEqual(answer, {"verdicts": []})
        self.assertEqual(["service_tier" in b for b in bodies], [True] * (1 + len(cp.FLEX_BACKOFF)) + [False])


class VerifierEffortTests(unittest.TestCase):
    def capture(self, model, effort, reject_reasoning=False):
        import io
        import urllib.error
        bodies = []

        def fake_send(request, timeout, attempts=3):
            body = json.loads(request.data)
            bodies.append(body)
            if reject_reasoning and "reasoning" in body:
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(
                    b"{\"error\": \"Unsupported value: 'reasoning.effort' does not support 'medium' with this model.\"}"))
            answer = {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"verdicts": []}'}]}]}
            return json.dumps(answer).encode(), {}
        saved, cp.send = cp.send, fake_send
        try:
            cp.call_responses(model, effort, "", "prompt", {"openai": "k"})
        finally:
            cp.send = saved
        return bodies

    def test_pro_models_get_high_effort(self):
        self.assertEqual(self.capture("gpt-5-pro", "medium")[0]["reasoning"], {"effort": "high"})

    def test_rejected_effort_is_dropped_and_retried(self):
        bodies = self.capture("gpt-6.1-sol", "medium", reject_reasoning=True)
        self.assertEqual(["reasoning" in b for b in bodies], [True, False])


class VerifierFormatTests(unittest.TestCase):
    def test_model_without_structured_outputs_falls_back_to_json_mode(self):
        import io
        import os
        import urllib.error
        bodies = []

        def fake_send(request, timeout, attempts=3):
            body = json.loads(request.data)
            bodies.append(body)
            if body["response_format"]["type"] == "json_schema":
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(
                    b"{\"error\": \"Invalid parameter: 'response_format' of type 'json_schema' is not supported with this model.\"}"))
            answer = {"choices": [{"message": {"content": json.dumps({"verdicts": [{"id": "1", "verdict": "fixed", "reason": "r"}]})}}]}
            return json.dumps(answer).encode(), {}
        saved, cp.send = cp.send, fake_send
        os.environ["SERVICE_TIER"] = ""
        try:
            answer, _ = cp.call_model("gpt-4-turbo", "medium", "prompt", {"openai": "k"})
        finally:
            cp.send = saved
        self.assertEqual(answer["verdicts"][0]["verdict"], "fixed")
        self.assertEqual([b["response_format"]["type"] for b in bodies], ["json_schema", "json_object"])
        self.assertIn("JSON object", bodies[1]["messages"][-1]["content"])


class VerifierEndpointTests(unittest.TestCase):
    def call(self, model, chat_rejects):
        import io
        import os
        import urllib.error
        urls = []

        def fake_send(request, timeout, attempts=3):
            urls.append(request.full_url)
            if request.full_url.endswith("/chat/completions"):
                if chat_rejects:
                    raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(
                        b'{"error": "This model is only supported in v1/responses and not in v1/chat/completions."}'))
                return json.dumps({"choices": [{"message": {"content": '{"verdicts": []}'}}]}).encode(), {}
            answer = {"output": [{"type": "reasoning"}, {"type": "message", "content": [
                {"type": "output_text", "text": '{"verdicts": [{"id": "1", "verdict": "fixed", "reason": "r"}]}'}]}]}
            return json.dumps(answer).encode(), {}
        saved, cp.send = cp.send, fake_send
        os.environ["SERVICE_TIER"] = ""
        try:
            answer, _ = cp.call_model(model, "high", "prompt", {"openai": "k"})
        finally:
            cp.send = saved
        return answer, urls

    def test_pro_models_go_to_the_responses_api(self):
        answer, urls = self.call("o3-pro", chat_rejects=True)
        self.assertEqual(urls, ["https://api.openai.com/v1/responses"])
        self.assertEqual(answer["verdicts"][0]["verdict"], "fixed")

    def test_a_model_chat_completions_rejects_falls_back_to_responses(self):
        answer, urls = self.call("gpt-9-preview", chat_rejects=True)
        self.assertEqual([u.rsplit("/", 1)[-1] for u in urls], ["completions", "responses"])
        self.assertEqual(answer["verdicts"][0]["verdict"], "fixed")

    def test_unparseable_model_output_is_an_empty_answer(self):
        self.assertEqual(cp.parse_json("not json"), {})


class VerdictTests(unittest.TestCase):
    def test_schema_invalid_verifier_output_is_uncertain_not_a_crash(self):
        for answer in ({"verdicts": [{"id": ["1"], "verdict": "fixed"}]}, {"verdicts": "fixed"},
                       {"verdicts": [{"id": "1", "verdict": ["fixed"]}]}, None, ["fixed"]):
            self.assertEqual(cp.parse_verdicts(answer, ["1"])["1"][0], "uncertain", answer)

    def test_repeated_verdicts_for_one_id_are_uncertain_and_carry_no_dependencies(self):
        answer = {"verdicts": [{"id": "1", "verdict": "fixed", "reason": "r", "relies_on": ["helper.py"]},
                               {"id": "1", "verdict": "fixed", "reason": "", "relies_on": []}]}
        self.assertEqual(cp.parse_verdicts(answer, ["1"])["1"][0], "uncertain")
        self.assertEqual(cp.relied_on(answer), {})
        single = {"verdicts": [{"id": "1", "verdict": "fixed", "reason": "r", "relies_on": ["./helper.py"]}]}
        self.assertEqual(cp.relied_on(single), {"1": ["helper.py"]})

    def test_verdict_without_a_reason_is_uncertain(self):
        for item in ({"id": "1", "verdict": "fixed"}, {"id": "1", "verdict": "fixed", "reason": ""},
                     {"id": "1", "verdict": "fixed", "reason": 7}):
            self.assertEqual(cp.parse_verdicts({"verdicts": [item]}, ["1"])["1"][0], "uncertain", item)

    def test_missing_unknown_and_malformed_verdicts_are_uncertain(self):
        verdicts = cp.parse_verdicts({"verdicts": [{"id": "1", "verdict": "fixed", "reason": "r", "relies_on": []},
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
        self.assertEqual(by_header["Leak"]["state"], "ACTIVE")       # y.py did not change: checked against the push only
        self.assertEqual(by_header["Overflow"]["state"], "ACTIVE")   # new blocking finding, still saved
        self.assertEqual(len(prompts), 2)
        self.assertIn("@@ fix", prompts[0])
        self.assertIn("files this push did not change", prompts[1])
        self.assertIn("Leak", prompts[1])

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
        self.assertEqual((race["state"], race["line"]), ("ACTIVE", 40))
        self.assertFalse(any("Race" in p for p in prompts))

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
                checkpoint_comment(github)["body"] = newer
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

    def test_full_review_for_a_security_concern_still_follows_renames(self):
        github = FakeGitHub(A, ["x.py", "y.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave."), security="Injection: y.py."))
        github.heads = [B]
        github.files = [{"filename": "moved.py", "patch": "@@"}, {"filename": "y.py", "patch": "@@"}]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}]}
        _, calls, _ = run_review(github, B, review(issue("moved.py", "Race", "Two saves interleave.")))
        self.assertEqual(calls[0]["mode"], "full")
        races = [f for f in github.state()["findings"] if f["header"] == "Race"]
        self.assertEqual(len(races), 1)
        self.assertEqual(races[0]["path"], "moved.py")
        self.assertEqual(sorted(i["path"] for i in races[0]["threads"]), ["moved.py", "x.py"])

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
        for bad in ({"key_issues_to_review": []}, {"security_concerns": "No"},
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
        moved_prompts = [p for p in prompts if p.startswith("File: moved.py")]
        self.assertEqual(len(moved_prompts), 1)
        self.assertIn("File: moved.py (renamed from x.py", moved_prompts[0])
        self.assertIn("the same racy code", moved_prompts[0])
        moved = {f["header"]: f for f in github.state()["findings"]}["Race"]
        self.assertEqual((moved["path"], moved["renamed_from"], moved["state"]), ("moved.py", "x.py", "ACTIVE"))
        self.assertEqual([i["path"] for i in moved["threads"]], ["x.py"])
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]
        self.assertTrue(cp.thread_posts(github.threads[0], moved))

    def test_issue_re_reported_at_a_renamed_path_stays_one_finding(self):
        github = self.first_run()
        github.heads = [B]
        github.files = [f for f in github.files if f["filename"] != "x.py"] + [{"filename": "moved.py", "patch": "@@"}]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}]}
        prompts = []
        run_review(github, B, review(issue("moved.py", "Race", "Two saves interleave.")), prompts=prompts)
        races = [f for f in github.state()["findings"] if f["header"] == "Race"]
        self.assertEqual(len(races), 1)
        self.assertFalse(any("Race" in p for p in prompts))
        self.assertEqual(sorted(i["path"] for i in races[0]["threads"]), ["moved.py", "x.py"])

    def test_same_head_retarget_runs_finishing_in_reverse_order_do_not_overwrite(self):
        github = self.first_run()
        github.base_ref = "release"
        newer = cp.render_comment({"revision": 2, "head_sha": A, "base_ref": "release", "merge_base": "m" * 40,
                                   "findings": [], "security": {}, "runs": []})
        calls = {"n": 0}
        original = github.comments

        def comments(repo, pr):
            # The faster retarget run saves revision 2 while this one is still reviewing.
            calls["n"] += 1
            if calls["n"] == 2:
                checkpoint_comment(github)["body"] = newer
            return original(repo, pr)
        github.comments = comments
        with self.assertRaises(cp.RunFailed):
            run_review(github, A, review())
        self.assertEqual(github.state()["revision"], 2)

    def test_base_changing_during_the_run_saves_nothing(self):
        github = self.first_run()
        saved = github.saved
        github.base_ref = "release"
        original = github.pull
        calls = {"n": 0}

        def pull(repo, pr):
            calls["n"] += 1
            if calls["n"] == 2:
                github.base_ref = "main"  # Retargeted back while the review ran.
            return original(repo, pr)
        github.pull = pull
        with self.assertRaises(cp.RunFailed):
            run_review(github, A, review())
        self.assertEqual(github.saved, saved)

    def test_distinct_findings_sharing_a_long_prefix_stay_separate(self):
        shared = "Saves interleave " + "z" * cp.CONTENT_CHARS
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(issue("x.py", "Race", shared + " on create.")))
        old = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        prompts = []
        run_review(github, B, review(issue("x.py", "Race", shared + " on delete.")), prompts=prompts)
        self.assertEqual(len(github.state()["findings"]), 2)
        self.assertEqual(len(prompts), 1)
        self.assertIn(old, prompts[0])

    def test_renamed_finding_reported_again_on_a_later_push_stays_one_finding(self):
        github = self.first_run()
        github.heads = [B]
        github.files = [f for f in github.files if f["filename"] != "x.py"] + [{"filename": "moved.py", "patch": "@@"}]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}]}
        race = finding_ids(github.state(), "x.py", "Race")[0]
        run_review(github, B, review(), verdicts={race: "still_present"})
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "moved.py"}]}
        prompts = []
        run_review(github, C, review(issue("moved.py", "Race", "Two saves interleave.")), prompts=prompts)
        races = [f for f in github.state()["findings"] if f["header"] == "Race"]
        self.assertEqual(len(races), 1)
        self.assertFalse(any("Race" in p for p in prompts))

    def test_overwritten_save_fails_the_run(self):
        github = FakeGitHub(A, ["x.py"])
        original = github.save_comment

        def save_then_overwritten(repo, pr, comment_id, body):
            comment = original(repo, pr, comment_id, body)
            # Another run's save lands right after this one.
            comment["body"] = cp.render_comment({"revision": 99, "head_sha": A, "findings": [], "security": {}, "runs": []})
            return comment
        github.save_comment = save_then_overwritten
        with self.assertRaises(cp.RunFailed):
            run_review(github, A, review())

    def test_files_left_out_are_reviewed_on_the_next_push(self):
        github = FakeGitHub(A, ["small.py", "big.py"])
        original_comments = github.comments

        def comments_with_footer(repo, pr):
            footer = {"user": {"login": "github-actions[bot]"}, "updated_at": "9999-01-01T00:00:00Z",
                      "body": "<!-- pr-agent:review:full -->\n⚠️ **Review coverage:** The following files were not "
                              "included in this review because of the token budget:\n- `big.py`"}
            return original_comments(repo, pr) + ([footer] if github.heads == [A] else [])
        github.comments = comments_with_footer
        run_review(github, A, review())
        self.assertEqual(github.state()["uncovered"], ["big.py"])
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "small.py"}]}
        _, calls, _ = run_review(github, B, review())
        self.assertEqual(calls[0]["files"], ["big.py", "small.py"])
        self.assertEqual(github.state()["uncovered"], [])

    def test_rename_survives_the_full_review_fallback_for_large_prs(self):
        others = [f"other{i}.py" for i in range(cp.MAX_IGNORED_FILES + 5)]
        github = FakeGitHub(A, ["x.py"] + others)
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.files = [f for f in github.files if f["filename"] != "x.py"] + [{"filename": "moved.py", "patch": "@@"}]
        github.contents["moved.py"] = "the same racy code"
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}]}
        prompts = []
        _, calls, _ = run_review(github, B, review(), verdicts={race: "still_present"}, prompts=prompts)
        self.assertEqual(calls[0]["mode"], "full")
        self.assertEqual(len(prompts), 1)
        self.assertIn("File: moved.py", prompts[0])
        self.assertIn("the same racy code", prompts[0])

    def test_file_with_a_trailing_space_left_out_is_retried_by_its_exact_name(self):
        github = FakeGitHub(A, ["small.py", "big.py "])
        original_comments = github.comments

        def comments_with_footer(repo, pr):
            footer = {"user": {"login": "github-actions[bot]"}, "updated_at": "9999-01-01T00:00:00Z",
                      "body": "<!-- pr-agent:review:full -->\n⚠️ **Review coverage:** The following files were not "
                              "included in this review because of the token budget:\n- ` big.py  `"}
            return original_comments(repo, pr) + ([footer] if github.heads == [A] else [])
        github.comments = comments_with_footer
        run_review(github, A, review())
        self.assertEqual(github.state()["uncovered"], ["big.py "])
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "small.py"}]}
        _, calls, _ = run_review(github, B, review())
        self.assertIn("big.py ", calls[0]["files"])

    def test_file_name_with_a_literal_backslash_n_left_out_is_retried(self):
        name = "src/a\\note.py"  # A real backslash followed by n.
        github = FakeGitHub(A, ["small.py", name])
        original_comments = github.comments

        def comments_with_footer(repo, pr):
            footer = {"user": {"login": "github-actions[bot]"}, "updated_at": "9999-01-01T00:00:00Z",
                      "body": "<!-- pr-agent:review:full -->\n⚠️ **Review coverage:** The following files were not "
                              "included in this review because of the token budget:\n- `" + name + "`"}
            return original_comments(repo, pr) + ([footer] if github.heads == [A] else [])
        github.comments = comments_with_footer
        run_review(github, A, review())
        self.assertEqual(github.state()["uncovered"], [name])
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "small.py"}]}
        _, calls, _ = run_review(github, B, review())
        self.assertIn(name, calls[0]["files"])

    def test_unlisted_coverage_gap_schedules_a_full_review_that_clears_it(self):
        github = FakeGitHub(A, ["small.py", "big.py"])
        original_comments = github.comments

        def comments_with_footer(repo, pr):
            footer = {"user": {"login": "github-actions[bot]"}, "updated_at": "9999-01-01T00:00:00Z",
                      "body": "<!-- pr-agent:review:full -->\n⚠️ **Review coverage:** The following files were not "
                              "included in this review because of the token budget:\n- `big.py`\n... and 3 more"}
            return original_comments(repo, pr) + ([footer] if github.heads == [A] else [])
        github.comments = comments_with_footer
        run_review(github, A, review())
        self.assertTrue(github.state()["uncovered_unlisted"])
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "small.py"}]}
        _, calls, _ = run_review(github, B, review())
        self.assertEqual(calls[0]["mode"], "full")  # Only a full review can name the unlisted files.
        state = github.state()
        self.assertEqual(state["uncovered"], [])
        self.assertFalse(state["uncovered_unlisted"])

    def test_renames_survive_a_comparison_capped_at_300_files(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.files = [{"filename": "moved.py", "patch": "@@"}] + [{"filename": f"f{i}.py"} for i in range(299)]
        github.contents["moved.py"] = "the same racy code"
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}] + [{"filename": f"f{i}.py"} for i in range(299)]}
        prompts = []
        _, calls, _ = run_review(github, B, review(), verdicts={race: "still_present"}, prompts=prompts)
        self.assertEqual(calls[0]["mode"], "full")
        self.assertIn("File: moved.py", prompts[0])

    def test_path_with_trailing_space_keeps_its_real_name(self):
        github = FakeGitHub(A, ["src/file.py "])
        run_review(github, A, review(issue("src/file.py", "Race", "Two saves interleave.")))
        finding = github.state()["findings"][0]
        self.assertEqual(finding["path"], "src/file.py ")
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "src/file.py "}]}
        prompts = []
        run_review(github, B, review(), verdicts={finding["id"]: "fixed"}, prompts=prompts)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_new_file_at_a_renamed_findings_old_path_gets_its_own_finding(self):
        github = self.first_run()
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.files = [f for f in github.files if f["filename"] != "x.py"] + [{"filename": "moved.py", "patch": "@@"}]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        # A new x.py arrives with the same wording of the same issue.
        github.heads = [C]
        github.files.append({"filename": "x.py", "patch": "@@"})
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, C, review(issue("x.py", "Race", "Two saves interleave.")))
        races = {f["path"]: f["state"] for f in github.state()["findings"] if f["header"] == "Race"}
        self.assertEqual(races, {"moved.py": "FIXED", "x.py": "ACTIVE"})

    def test_vanished_file_after_a_capped_comparison_is_not_verified_as_fixed(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        github.heads = [B]
        github.files = [{"filename": f"f{i}.py"} for i in range(300)]
        github.contents["x.py"] = None  # Renamed somewhere beyond the 300 files GitHub listed.
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]}
        prompts = []
        _, calls, _ = run_review(github, B, review(), prompts=prompts)
        self.assertEqual((calls[0]["mode"], prompts), ("full", []))
        race = github.state()["findings"][0]
        self.assertEqual((race["state"], race["check"]["verdict"]), ("ACTIVE", "uncertain"))

    def test_finding_fixed_by_a_change_in_another_file_is_rechecked_and_cleared(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "helper.py", "patch": "-    return None\n+    return ''"}]}
        prompts = []
        run_review(github, B, review(), verdicts={null: "fixed"}, prompts=prompts)
        self.assertEqual(len(prompts), 1)
        self.assertIn("files this push did not change", prompts[0])
        self.assertIn("+    return ''", prompts[0])
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_vanished_file_after_a_force_push_or_missing_comparison_stays_open(self):
        for comparison in ({"status": "diverged", "files": []}, None):
            github = FakeGitHub(A, ["x.py"])
            run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
            github.heads = [B]
            github.files = [{"filename": "elsewhere.py"}]
            github.contents["x.py"] = None
            github.compares[(A, B)] = comparison
            prompts = []
            _, calls, _ = run_review(github, B, review(), prompts=prompts)
            self.assertEqual((calls[0]["mode"], prompts), ("full", []), comparison)
            self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE", comparison)

    def test_verifying_a_changed_file_also_sees_the_push_s_other_changes(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "caller.py", "patch": "-x  = 1\n+x = 1"},
            {"filename": "helper.py", "patch": "-    return None\n+    return ''"}]}
        prompts = []
        run_review(github, B, review(), verdicts={null: "fixed"}, prompts=prompts)
        self.assertEqual(len(prompts), 1)
        self.assertTrue(prompts[0].startswith("File: caller.py"))
        self.assertIn("+    return ''", prompts[0])
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_an_uncertain_verdict_keeps_the_fix_in_the_next_verification(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "helper.py", "patch": "-    return None\n+    return ''"}]}
        run_review(github, B, review())  # The verifier gives no verdict: uncertain.
        finding = github.state()["findings"][0]
        self.assertEqual((finding["check"]["verdict"], finding["checked_sha"]), ("uncertain", A))
        github.heads = [C]
        github.compares[(A, C)] = {"status": "ahead", "files": [
            {"filename": "caller.py", "patch": "-x  = 1\n+x = 1"},
            {"filename": "helper.py", "patch": "-    return None\n+    return ''"}]}
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "-x  = 1\n+x = 1"}]}
        prompts = []
        run_review(github, C, review(), prompts=prompts)
        self.assertEqual(len(prompts), 1)
        self.assertIn("+    return ''", prompts[0])

    def test_unknown_rename_survives_a_same_head_rerun(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        race = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.files = [{"filename": f"f{i}.py"} for i in range(300)]
        github.contents["x.py"] = None
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]}
        run_review(github, B, review())
        prompts = []
        run_review(github, B, review(), verdicts={race: "fixed"}, prompts=prompts)  # Rerun at the same head.
        finding = github.state()["findings"][0]
        self.assertEqual((finding["state"], prompts), ("ACTIVE", []))
        self.assertTrue(finding["rename_unknown"])

    def test_capped_comparison_keeps_the_missing_file_safeguard_in_any_full_review(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave."), security="Injection: x.py."))
        github.heads = [B]
        github.files = [{"filename": f"f{i}.py"} for i in range(300)]
        github.contents["x.py"] = None
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]}
        prompts = []
        _, calls, _ = run_review(github, B, review(), prompts=prompts)
        self.assertEqual(calls[0]["reason"], "an earlier review reported a security concern")
        self.assertEqual(prompts, [])
        self.assertEqual(github.state()["findings"][0]["check"]["verdict"], "uncertain")

    def test_recreated_file_at_a_renamed_findings_old_path_gets_a_distinct_id(self):
        github = self.first_run()
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.files = [f for f in github.files if f["filename"] != "x.py"] + [{"filename": "moved.py", "patch": "@@"}]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "moved.py", "previous_filename": "x.py", "status": "renamed"}]}
        run_review(github, B, review(), verdicts={race: "still_present"})
        github.heads = [C]
        github.files.append({"filename": "x.py", "patch": "@@"})
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, C, review(issue("x.py", "Race", "Two saves interleave.")))
        races = [f for f in github.state()["findings"] if f["header"] == "Race"]
        self.assertEqual(sorted(f["path"] for f in races), ["moved.py", "x.py"])
        self.assertEqual(len({f["id"] for f in races}), 2)
        # A later helper change fixes only the moved copy; the other keeps its own verdict.
        github.heads = ["d" * 40]
        github.files.append({"filename": "helper.py", "patch": "@@"})
        github.compares[(C, "d" * 40)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "+fix"}]}
        moved_id = next(f["id"] for f in races if f["path"] == "moved.py")
        run_review(github, "d" * 40, review(), verdicts={moved_id: "fixed"})
        states = {f["path"]: f["state"] for f in github.state()["findings"] if f["header"] == "Race"}
        self.assertEqual(states, {"moved.py": "FIXED", "x.py": "ACTIVE"})
        # The original thread on x.py was resolved for the moved finding; it cannot dismiss the new one.
        github.threads = [posted("T_orig", "x.py", "Race", "Two saves interleave.", resolved=True)]
        self.assertEqual(cp.gate_main(github, "o/r", "1", "d" * 40, out=lambda _: None, sleep=lambda _: None), 1)

    def test_findings_with_different_check_commits_keep_their_own_ranges(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None.")))
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "+    return ''"}]}
        # The helper fix gets no verdict (uncertain, still checked at A); the review adds a second finding.
        run_review(github, B, review(issue("caller.py", "Leak", "The file handle is never closed.")))
        github.heads = [C]
        github.compares[(A, C)] = {"status": "ahead", "files": [
            {"filename": "caller.py", "patch": "+tidy"}, {"filename": "helper.py", "patch": "+    return ''"}]}
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "+tidy"}]}
        prompts = []
        run_review(github, C, review(), prompts=prompts)
        null_prompt = next(p for p in prompts if "helper() can return None" in p)
        self.assertIn("+    return ''", null_prompt)
        self.assertEqual(len(prompts), 2)

    def test_delta_of_only_binary_files_with_no_review_output_advances(self):
        github = FakeGitHub(A, ["x.py", "assets/image"])
        run_review(github, A, review())
        github.files = [{"filename": "x.py", "patch": "@@"}, {"filename": "assets/image"}]  # No text diff.
        github.binary = {"assets/image"}
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "assets/image"}]}
        code, calls, _ = run_review(github, B, cp.NoReviewOutput("PR-Agent produced no review output."))
        self.assertEqual((code, calls[0]["files"]), (0, ["assets/image"]))
        self.assertEqual(github.state()["head_sha"], B)

    def test_deleting_a_binary_file_counts_as_nothing_to_review(self):
        github = FakeGitHub(A, ["x.py", "assets/image"])
        run_review(github, A, review())
        github.files = [{"filename": "x.py", "patch": "@@"}, {"filename": "assets/image", "status": "removed"}]
        github.contents[("assets/image", B)] = None  # Gone at the head.
        github.binary_at = {("assets/image", A): True, ("assets/image", B): False}
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "assets/image", "status": "removed"}]}
        code, _, _ = run_review(github, B, cp.NoReviewOutput("PR-Agent produced no review output."))
        self.assertEqual((code, github.state()["head_sha"]), (0, B))

    def test_large_text_file_without_a_patch_is_not_nothing_to_review(self):
        github = FakeGitHub(A, ["x.py", "big.py"])
        run_review(github, A, review())
        github.files = [{"filename": "x.py", "patch": "@@"}, {"filename": "big.py"}]  # Too large for a GitHub patch.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "big.py"}]}
        with self.assertRaises(cp.RunFailed):
            run_review(github, B, cp.NoReviewOutput("PR-Agent produced no review output."))
        self.assertEqual(github.state()["head_sha"], A)

    def test_no_review_output_for_a_text_change_still_fails(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review())
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        with self.assertRaises(cp.RunFailed):
            run_review(github, B, cp.NoReviewOutput("PR-Agent produced no review output."))

    def test_base_reset_during_the_run_saves_nothing(self):
        github = self.first_run()
        saved = github.saved
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "z.py"}]}
        original = github.pull
        calls = {"n": 0}

        def pull(repo, pr):
            calls["n"] += 1
            if calls["n"] == 2:  # The base branch is force-reset behind the fork point mid-run.
                github.base_sha = "reset"
                github.compares[("reset", B)] = {"status": "ahead", "files": [], "merge_base_commit": {"sha": "n" * 40}}
            return original(repo, pr)
        github.pull = pull
        with self.assertRaises(cp.RunFailed):
            run_review(github, B, review())
        self.assertEqual(github.saved, saved)

    def test_fix_beyond_a_capped_comparison_reaches_the_verifier(self):
        github = FakeGitHub(A, ["x.py"] + [f"f{i}.py" for i in range(300)])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]}
        github.contents[("x.py", A)] = "save()\n"
        github.contents[("x.py", B)] = "with lock:\n    save()\n"
        prompts = []
        run_review(github, B, review(), prompts=prompts)
        x_prompt = next(p for p in prompts if p.startswith("File: x.py"))
        self.assertIn("+with lock:", x_prompt)  # This file's own diff for the range, not its whole PR patch.
        self.assertNotIn("@@ patch of x.py", x_prompt)
        self.assertIn("since the findings were last checked", x_prompt)

    def test_saved_checkpoint_read_back_tolerates_a_stale_first_read(self):
        github = FakeGitHub(A, ["x.py"])
        original = github.comments
        served_stale = {"done": False}

        def comments(repo, pr):
            # The first read after the save still shows the comment from before it.
            if github.saved and not served_stale["done"]:
                served_stale["done"] = True
                return []
            return original(repo, pr)
        github.comments = comments
        code, _, _ = run_review(github, A, review())
        self.assertEqual(code, 0)
        self.assertTrue(served_stale["done"])

    def test_rerun_at_the_same_head_retries_uncertain_verifications(self):
        github = self.first_run()
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, B, review())  # The verifier gives no verdict: uncertain at B.
        self.assertEqual({f["header"]: f for f in github.state()["findings"]}["Race"]["check"]["verdict"], "uncertain")
        code, calls, _ = run_review(github, B, review(), verdicts={race: "fixed"})  # Re-run the workflow.
        self.assertEqual((code, calls), (0, []))  # No new PR-Agent review.
        self.assertEqual({f["header"]: f for f in github.state()["findings"]}["Race"]["state"], "FIXED")

    def test_coverage_read_waits_for_this_runs_summary(self):
        github = FakeGitHub(A, ["small.py", "big.py"])
        original = github.comments
        reads = {"n": 0}

        def comments(repo, pr):
            listed = original(repo, pr)
            summaries = [c for c in listed if "pr-agent:review" in c["body"]]
            if not summaries:
                return listed
            reads["n"] += 1
            if reads["n"] == 1:  # Stale: the summary from before this run.
                return [c for c in listed if c not in summaries] + [dict(summaries[-1], updated_at="2000-01-01T00:00:00Z")]
            footer = dict(summaries[-1], body=summaries[-1]["body"] + "\n⚠️ **Review coverage:** The following files "
                          "were not included in this review because of the token budget:\n- `big.py`")
            return [c for c in listed if c not in summaries] + [footer]
        github.comments = comments
        run_review(github, A, review())
        self.assertEqual(github.state()["uncovered"], ["big.py"])

    def test_reverting_a_fix_in_another_file_reopens_the_finding(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "+    return ''"}]}
        run_review(github, B, review(), verdicts={null: "fixed"}, relies={null: ["helper.py"]})
        fixed = github.state()["findings"][0]
        self.assertEqual((fixed["state"], fixed["fix_files"]), ("FIXED", ["helper.py"]))
        # The helper change is reverted to its base version, so helper.py leaves the PR diff entirely.
        github.files = [{"filename": "caller.py", "patch": "@@"}]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "-    return ''"}]}
        prompts = []
        _, calls, _ = run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertEqual(calls, [])  # Nothing for PR-Agent to review.
        self.assertIn("fixed earlier", prompts[0])
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")
        self.assertEqual(cp.gate_main(github, "o/r", "1", C, out=lambda _: None, sleep=lambda _: None), 1)

    def helper_fixed(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "+    return ''"}]}
        run_review(github, B, review(), verdicts={null: "fixed"}, relies={null: ["helper.py"]})
        return github, null

    def test_reverted_fix_is_rechecked_even_after_the_fixed_record_was_pruned(self):
        github, null = self.helper_fixed()
        state = github.state()
        record = state["findings"].pop()  # Pruned for size, as fit_comment would.
        state["fixed_deps"] = [{"id": record["id"], "path": record["path"], "header": record["header"],
                                "content": record["content"][:300], "content_id": record["content_id"],
                                "fix_files": record["fix_files"], "threads": record["threads"]}]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.files = [{"filename": "caller.py", "patch": "@@"}]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "-    return ''"}]}
        prompts = []
        run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertIn("fixed earlier", prompts[0])
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_inconclusive_recheck_blocks_and_a_rerun_retries_it(self):
        github, null = self.helper_fixed()
        github.files = [{"filename": "caller.py", "patch": "@@"}]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "-    return ''"}]}
        run_review(github, C, review())  # The verifier gives no verdict.
        finding = github.state()["findings"][0]
        self.assertEqual((finding["state"], finding["check"]["verdict"]), ("ACTIVE", "uncertain"))
        self.assertEqual(cp.gate_main(github, "o/r", "1", C, out=lambda _: None, sleep=lambda _: None), 1)
        code, calls, _ = run_review(github, C, review(), verdicts={null: "fixed"})  # Re-run at the same head.
        self.assertEqual((code, calls), (0, []))
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_confirmed_recheck_refreshes_what_the_fix_relies_on(self):
        github, null = self.helper_fixed()
        github.files = [{"filename": "caller.py", "patch": "@@"}, {"filename": "helper_b.py", "patch": "@@"}]
        github.heads = [C]
        # A refactor moves the fix into helper_b.py.
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "-moved"},
                                                                 {"filename": "helper_b.py", "patch": "+    return ''"}]}
        run_review(github, C, review(), verdicts={null: "fixed"}, relies={null: ["helper_b.py"]})
        self.assertEqual(github.state()["findings"][0]["fix_files"], ["helper.py", "helper_b.py"])
        github.files = [{"filename": "caller.py", "patch": "@@"}]
        github.heads = ["d" * 40]
        github.compares[(C, "d" * 40)] = {"status": "ahead", "files": [{"filename": "helper_b.py", "patch": "-    return ''"}]}
        prompts = []
        run_review(github, "d" * 40, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_capped_comparison_rechecks_tracked_fixes(self):
        github, null = self.helper_fixed()
        github.files = [{"filename": "caller.py", "patch": "@@"}] + [{"filename": f"f{i}.py"} for i in range(300)]
        github.heads = [C]
        # The helper revert falls beyond the 300 files GitHub lists.
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]}
        prompts = []
        run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_fixed_verdict_without_relies_on_is_uncertain(self):
        for item in ({"id": "1", "verdict": "fixed", "reason": "r"},
                     {"id": "1", "verdict": "fixed", "reason": "r", "relies_on": "helper.py"},
                     {"id": "1", "verdict": "fixed", "reason": "r", "relies_on": [7]}):
            self.assertEqual(cp.parse_verdicts({"verdicts": [item]}, ["1"])["1"][0], "uncertain", item)

    def test_fixed_finding_whose_file_is_reverted_out_of_the_pr_is_rechecked(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "+if x:"}]}
        run_review(github, B, review(), verdicts={null: "fixed"})  # Fixed in its own file.
        github.files = [{"filename": "helper.py", "patch": "@@"}]  # caller.py reverted to its base version.
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "-if x:"}]}
        prompts = []
        run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_pruned_own_file_fix_reverted_out_of_the_pr_is_rechecked(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "+if x:"}]}
        run_review(github, B, review(), verdicts={null: "fixed"})
        state = github.state()
        # Just under the current size, so the fixed record has to be pruned.
        saved, cp.COMMENT_LIMIT = cp.COMMENT_LIMIT, len(cp.render_comment(state)) - 50
        try:
            checkpoint_comment(github)["body"] = cp.fit_comment(state)
        finally:
            cp.COMMENT_LIMIT = saved
        self.assertEqual(github.state()["findings"], [])
        self.assertEqual(github.state()["fixed_own"][0]["path"], "caller.py")
        github.files = [{"filename": "helper.py", "patch": "@@"}]  # caller.py reverted out of the PR.
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "-if x:"}]}
        prompts = []
        run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_own_file_revert_beyond_a_capped_comparison_is_rechecked(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "+if x:"}]}
        run_review(github, B, review(), verdicts={null: "fixed"})
        github.files = [{"filename": "helper.py", "patch": "@@"}] + [{"filename": f"f{i}.py"} for i in range(300)]
        github.heads = [C]
        # caller.py was reverted out of the PR, beyond the 300 files GitHub lists.
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]}
        prompts = []
        run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_dependency_named_with_a_dot_slash_is_kept(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "+    return ''"}]}
        run_review(github, B, review(), verdicts={null: "fixed"}, relies={null: ["./helper.py"]})
        self.assertEqual(github.state()["findings"][0]["fix_files"], ["helper.py"])
        github.files = [{"filename": "caller.py", "patch": "@@"}]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "-    return ''"}]}
        prompts = []
        run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))

    def test_dependency_with_a_trailing_space_keeps_its_exact_name(self):
        self.assertEqual(cp.normalize_path("./helper.py "), "helper.py ")
        self.assertEqual(cp.normalize_path("src\\a.py"), "src\\a.py")
        github = FakeGitHub(A, ["caller.py", "helper.py "])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "helper.py ", "patch": "+    return ''"}]}
        run_review(github, B, review(), verdicts={null: "fixed"}, relies={null: ["helper.py "]})
        self.assertEqual(github.state()["findings"][0]["fix_files"], ["helper.py "])
        github.files = [{"filename": "caller.py", "patch": "@@"}]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "helper.py ", "patch": "-    return ''"}]}
        prompts = []
        run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))

    def test_own_file_fix_renamed_to_an_unreviewed_file_is_rechecked(self):
        github = FakeGitHub(A, ["app.js"])
        run_review(github, A, review(issue("app.js", "Null", "render() reads a missing key.")))
        null = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "app.js", "patch": "+guard"}]}
        run_review(github, B, review(), verdicts={null: "fixed"})
        github.files = [{"filename": "app.min.js", "patch": "@@"}]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [
            {"filename": "app.min.js", "previous_filename": "app.js", "status": "renamed", "patch": "-guard"}]}
        prompts = []
        _, calls, _ = run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertEqual(calls, [])  # The minified file is not reviewed.
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_binary_only_push_leaves_no_coverage_gap(self):
        github = FakeGitHub(A, ["x.py", "assets/image"])
        run_review(github, A, review())
        github.files = [{"filename": "x.py", "patch": "@@"}, {"filename": "assets/image"}]
        github.binary = {"assets/image"}
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "assets/image"}]}
        run_review(github, B, cp.NoReviewOutput("PR-Agent produced no review output."))
        self.assertFalse(github.state()["uncovered_unlisted"])
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        _, calls, _ = run_review(github, C, review())
        self.assertEqual(calls[0]["mode"], "delta")

    def test_rewind_to_before_a_fix_reopens_it_when_no_diff_can_be_shown(self):
        github, null = self.helper_fixed()
        github.heads = [A]  # Force-pushed back to the commit before the fix.
        github.compares[(B, A)] = {"status": "behind", "files": []}
        prompts = []
        _, calls, _ = run_review(github, A, review(), prompts=prompts)
        self.assertEqual(calls[0]["mode"], "full")
        finding = github.state()["findings"][0]
        self.assertEqual((finding["state"], finding["check"]["verdict"]), ("ACTIVE", "uncertain"))

    def test_history_trimming_never_drops_fix_dependencies(self):
        deps = [{"id": str(i), "path": "caller.py", "header": "H", "content": "c", "fix_files": ["helper.py"],
                 "threads": []} for i in range(5)]
        state = {"fixed_ids": [f"{i:012x}" for i in range(3000)], "fixed_deps": list(deps)}
        cp.bound_history(state)
        self.assertEqual(state["fixed_deps"], deps)
        self.assertTrue(state["history_trimmed"])

    def test_same_head_full_rerun_does_not_reopen_fixed_findings(self):
        github, null = self.helper_fixed()
        state = github.state()
        state["security"] = {"sha": B, "concern": "Injection: something."}  # Forces a full review.
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        _, calls, _ = run_review(github, B, review())  # Rerun at the same head.
        self.assertEqual(calls[0]["mode"], "full")
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_pruned_dependency_fix_is_rechecked_when_its_ignored_own_file_changes(self):
        github, null = self.helper_fixed()
        state = github.state()
        record = state["findings"].pop()
        state["fixed_deps"] = [{"id": record["id"], "path": "caller.py", "header": record["header"],
                                "content": record["content"][:200], "content_id": record["content_id"],
                                "fix_files": record["fix_files"], "threads": record["threads"]}]
        state["ignore_glob"] = ["caller.py"]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "caller.py", "patch": "-if x:"}]}
        prompts = []
        _, calls, _ = run_review(github, C, review(), verdicts={null: "still_present"}, prompts=prompts,
                                 globs=["caller.py"])
        self.assertEqual(calls, [])  # The ignored file is not reviewed.
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_fixed_file_left_out_for_the_token_budget_is_rechecked(self):
        github = FakeGitHub(A, ["x.py", "y.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        race = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        original = github.comments

        def with_gap(repo, pr):
            listed = original(repo, pr)
            if github.heads != [C]:
                return listed
            return listed + [{"user": {"login": "github-actions[bot]"}, "updated_at": "9999-01-01T00:00:00Z",
                              "body": "<!-- pr-agent:review:full -->\n⚠️ **Review coverage:** The following files were "
                                      "not included in this review because of the token budget:\n- `x.py`"}]
        github.comments = with_gap
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "-lock"}]}
        prompts = []
        run_review(github, C, review(), verdicts={race: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def footer_for(self, github, head, listing):
        original = github.comments

        def with_footer(repo, pr):
            listed = original(repo, pr)
            if github.heads != [head]:
                return listed
            return listed + [{"user": {"login": "github-actions[bot]"}, "updated_at": "9999-01-01T00:00:00Z",
                              "body": "<!-- pr-agent:review:full -->\n⚠️ **Review coverage:** The following files were "
                                      "not included in this review because of the token budget:\n" + listing}]
        github.comments = with_footer

    def own_file_fixed(self):
        github = FakeGitHub(A, ["x.py", "y.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        race = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        return github, race

    def test_fixed_file_among_unnamed_coverage_gaps_is_rechecked(self):
        github, race = self.own_file_fixed()
        self.footer_for(github, C, "- `y.py`\n... and 3 more")
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "-lock"}]}
        prompts = []
        run_review(github, C, review(), verdicts={race: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_pruned_own_file_fix_skipped_for_the_token_budget_is_rechecked(self):
        github, race = self.own_file_fixed()
        state = github.state()
        saved, cp.COMMENT_LIMIT = cp.COMMENT_LIMIT, len(cp.render_comment(state)) - 50
        try:
            checkpoint_comment(github)["body"] = cp.fit_comment(state)
        finally:
            cp.COMMENT_LIMIT = saved
        self.assertEqual(github.state()["findings"], [])
        self.footer_for(github, C, "- `x.py`")
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "-lock"}]}
        prompts = []
        run_review(github, C, review(), verdicts={race: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_verifier_gets_the_real_diff_after_a_diverged_force_push(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        github.heads = [B]
        github.compares[(A, B)] = {"status": "diverged", "files": []}  # GitHub compares from the merge base.
        github.contents[("x.py", A)] = "save()\n"
        github.contents[("x.py", B)] = "with lock:\n    save()\n"
        prompts = []
        run_review(github, B, review(), prompts=prompts)
        x_prompt = next(p for p in prompts if p.startswith("File: x.py"))
        self.assertIn("+with lock:", x_prompt)

    def test_cross_file_evidence_after_a_diverged_force_push_is_the_real_change(self):
        github = FakeGitHub(A, ["caller.py", "helper.py"])
        run_review(github, A, review(issue("caller.py", "Null", "helper() can return None, and .upper() fails.")))
        github.heads = [B]
        # Force-pushed: GitHub compares from the merge base, which hides what changed in helper.py.
        github.compares[(A, B)] = {"status": "diverged", "files": [{"filename": "caller.py"}]}
        github.compares[(B, A)] = {"status": "diverged", "files": [{"filename": "helper.py"}]}
        github.contents[("helper.py", A)] = "return None\n"
        github.contents[("helper.py", B)] = "return ''\n"
        prompts = []
        run_review(github, B, review(), prompts=prompts)
        self.assertTrue(any("+return ''" in p and "-return None" in p for p in prompts))

    def test_fix_dependencies_follow_renames(self):
        github, null = self.helper_fixed()
        github.files = [{"filename": "caller.py", "patch": "@@"}, {"filename": "utils.py", "patch": "@@"}]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [
            {"filename": "utils.py", "previous_filename": "helper.py", "status": "renamed"}]}
        run_review(github, C, review(), verdicts={null: "fixed"})
        self.assertEqual(github.state()["findings"][0]["fix_files"], ["utils.py"])
        github.heads = ["d" * 40]
        github.compares[(C, "d" * 40)] = {"status": "ahead", "files": [{"filename": "utils.py", "patch": "-    return ''"}]}
        prompts = []
        run_review(github, "d" * 40, review(), verdicts={null: "still_present"}, prompts=prompts)
        self.assertTrue(any("fixed earlier" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_unknown_rename_is_kept_out_of_the_cross_file_check_too(self):
        github = FakeGitHub(A, ["x.py", "y.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))
        race = github.state()["findings"][0]["id"]
        github.heads = [B]
        github.files = [{"filename": "y.py"}] + [{"filename": f"f{i}.py"} for i in range(300)]
        github.contents["x.py"] = None
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]}
        run_review(github, B, review())
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "y.py", "patch": "+other"}]}
        prompts = []
        run_review(github, C, review(), verdicts={race: "fixed"}, prompts=prompts)
        self.assertFalse(any("Two saves interleave" in p for p in prompts))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")

    def test_security_concern_is_recorded(self):
        github = FakeGitHub(A, ["x.py"])
        run_review(github, A, review(security="SQL injection: the query concatenates input."))
        self.assertTrue(github.state()["security"]["concern"].startswith("SQL injection"))


class PrAgentProcessTests(unittest.TestCase):
    """Runs run_pr_agent against a stand-in PR-Agent that reports the environment it got."""

    def run_with(self, model, keys, plan_result, pr_paths, exit_code=0, write_review=True, flex_busy=False,
                 chunked=False):
        import os
        import sys
        import tempfile
        probe = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        runs = probe.name + ".runs"
        probe.write(textwrap.dedent(f"""
            import json, os
            with open({runs!r}, "a") as count:
                count.write("run\\n")
            with open({runs!r}) as count:
                run = len(count.read().split())
            seen = {{k: os.environ.get(k) for k in ("OPENAI_KEY", "ANTHROPIC_KEY", "ANTHROPIC.KEY", "IGNORE.GLOB", "LITELLM.EXTRA_BODY",
                                                     "PR_REVIEWER.PERSISTENT_FINDING_STATE", "PR_REVIEWER.PERSISTENT_COMMENT")}}
            if {chunked!r}:
                print("Number of PR chunk calls: 2")
            if run <= {int(flex_busy) if flex_busy is not True else 99} and os.environ.get("LITELLM.EXTRA_BODY"):
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
            if os.path.exists(runs):
                os.unlink(runs)

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

    def test_flex_that_frees_up_on_a_retry_reviews_on_flex(self):
        seen = self.run_with("gpt-6.1-sol", {"anthropic": "", "openai": "o-key"},
                             {"mode": "full", "files": ["x.py"]}, {"x.py"}, flex_busy=2)["seen"]
        self.assertEqual(json.loads(seen["LITELLM.EXTRA_BODY"]), {"service_tier": "flex"})

    def test_chunked_review_is_not_rerun_after_flex_runs_out(self):
        with self.assertRaises(cp.RunFailed) as failure:
            self.run_with("gpt-6.1-sol", {"anthropic": "", "openai": "o-key"},
                          {"mode": "full", "files": ["x.py"]}, {"x.py"}, flex_busy=True, chunked=True)
        self.assertIn("partway through a chunked review", str(failure.exception))

    def test_chunk_count_is_seen_even_when_the_log_is_long(self):
        import sys
        import tempfile
        import os
        probe = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        probe.write("print('Number of PR chunk calls: 3')\nfor i in range(5000):\n    print('line', i)\n"
                    "print('Flex does not have sufficient resources available')\nraise SystemExit(1)\n")
        probe.close()
        try:
            code, tail, chunks = cp.run_streaming([sys.executable, probe.name], dict(os.environ))
        finally:
            os.unlink(probe.name)
        self.assertEqual((code, chunks), (1, 3))
        self.assertNotIn("Number of PR chunk calls", tail)  # Gone from the tail, still counted.

    def test_only_a_tier_rejection_counts_as_flex_refused(self):
        self.assertTrue(cp.flex_refused("Invalid value for 'service_tier': flex is not supported"))
        self.assertTrue(cp.flex_refused("Flex does not have sufficient resources available"))
        self.assertFalse(cp.flex_refused("timeout while waiting; request had service_tier=flex"))

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
        github.threads = [current(posted("T1", "x.py", "Possible Issue", "Two saves interleave."))]
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
        github.threads = [current(posted("T_old", "x.py", "Race", shared + " on create.", resolved=True))]
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("1 open PR-Agent finding", output)

    def test_finding_without_a_fingerprint_counts_as_summary_only(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        comment = checkpoint_comment(github)
        state = github.state()
        state["findings"][0]["threads"] = []
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
            # The findings job runs after every review and resolves the threads of fixed findings,
            # including those already pruned from the checkpoint.
            self.gate(github, head=head)
            previous = head
        self.assertGreater(github.state()["pruned_fixed"], 0)
        # Every thread but the ten of the last round, which are still open findings, is resolved.
        self.assertEqual(len(github.resolved), 110)
        self.assertFalse(any(t.startswith("T11-") for t in github.resolved))

    def test_pruned_identity_is_dropped_once_its_threads_are_resolved(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        comment = checkpoint_comment(github)
        state = github.state()
        state["pruned"] = [{"path": "x.py", "fingerprint": "aaaaaaaaaaaa"}, {"path": "y.py", "fingerprint": "bbbbbbbbbbbb"}]
        comment["body"] = cp.render_comment(state)
        github.threads = [thread("T1", "x.py", "<!-- pr-agent-dedup: aaaaaaaaaaaa -->"),
                          thread("T2", "y.py", "<!-- pr-agent-dedup: bbbbbbbbbbbb -->", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "z.py"}]}
        run_review(github, B, review())
        self.assertEqual([p["fingerprint"] for p in github.state()["pruned"]], ["aaaaaaaaaaaa"])

    def test_reintroduced_finding_is_not_dismissed_by_its_old_resolved_thread(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, C, review(issue("x.py", "Race", "Two saves interleave.")))
        code, output = self.gate(github, head=C)
        self.assertEqual(code, 1)
        self.assertIn("Race: Two saves interleave.", output)

    def test_reintroduced_finding_is_dismissed_by_a_reply_and_resolution(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, C, review(issue("x.py", "Race", "Two saves interleave.")))
        # A collaborator replies "dismiss" on the old thread after the finding came back, and resolves it.
        reply = posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True, last_comment_at="2999-01-01T00:00:00Z")
        reply.update(last_comment_body="/dismiss intended, see the design doc.", last_comment_association="COLLABORATOR")
        github.threads = [reply]
        self.assertEqual(self.gate(github, head=C)[0], 0)

    def test_a_plain_reply_or_an_outsider_cannot_dismiss_a_reintroduction(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, C, review(issue("x.py", "Race", "Two saves interleave.")))
        for body, association in (("This is still broken.", "COLLABORATOR"), ("/dismiss", "NONE"),
                                  ("Do not dismiss: this still fails", "COLLABORATOR")):
            reply = posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True,
                           last_comment_at="2999-01-01T00:00:00Z")
            reply.update(last_comment_body=body, last_comment_association=association)
            github.threads = [reply]
            self.assertEqual(self.gate(github, head=C)[0], 1, (body, association))

    def test_finding_raised_again_after_its_record_was_pruned_counts_as_reintroduced(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        state = github.state()
        record = state["findings"].pop()
        state["pruned"] = [{"id": record["id"], "threads": record["threads"]}]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        # Raised again with no line, so PR-Agent posts no new thread.
        run_review(github, B, review({"relevant_file": "x.py", "issue_header": "Race",
                                      "issue_content": "Two saves interleave."}))
        new = {f["header"]: f for f in github.state()["findings"]}["Race"]
        self.assertTrue(new.get("reopened_at"))
        self.assertEqual(github.state()["pruned"], [])
        code, _ = self.gate(github, head=B)
        self.assertEqual(code, 1)

    def test_new_thread_for_a_reintroduced_finding_dismisses_it_when_resolved(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        before = cp.now_utc()
        run_review(github, C, review(issue("x.py", "Race", "Two saves interleave.", line=30)))
        reopened_at = {f["header"]: f for f in github.state()["findings"]}["Race"]["reopened_at"]
        self.assertLessEqual(reopened_at, before)  # Timed from the run's start, before PR-Agent posted.
        # PR-Agent posted a new thread during the run, after it started; it is resolved without a reply.
        new_thread = posted("T_new", "x.py", "Race", "Two saves interleave.", resolved=True)
        new_thread["created_at"] = "9999-01-01T00:00:00Z"
        github.threads = [posted("T_old", "x.py", "Race", "Two saves interleave.", resolved=True), new_thread]
        self.assertEqual(self.gate(github, head=C)[0], 0)

    def test_thread_posted_by_a_failed_attempt_dismisses_the_reintroduction_after_a_retry(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        fixed_saved_at = github.state()["saved_at"]
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        # A first attempt at C posts PR-Agent's thread, then fails before saving.
        with self.assertRaises(cp.RunFailed):
            run_review(github, C, cp.RunFailed("PR-Agent exited with code 1."))
        attempt_thread = posted("T_attempt", "x.py", "Race", "Two saves interleave.", resolved=True)
        attempt_thread["created_at"] = fixed_saved_at[:-1] + ".5Z"  # Just after the fix was saved.
        # The retry succeeds; PR-Agent deduplicates and posts no new thread.
        run_review(github, C, review(issue("x.py", "Race", "Two saves interleave.")))
        self.assertEqual({f["header"]: f for f in github.state()["findings"]}["Race"]["reopened_at"], fixed_saved_at)
        github.threads = [attempt_thread]
        self.assertEqual(self.gate(github, head=C)[0], 0)

    def test_new_finding_is_not_dismissed_by_a_thread_from_an_evicted_earlier_occurrence(self):
        github = self.reviewed()
        state = github.state()
        state["fixed_ids"], state["history_trimmed"] = [], True  # Its fixed history was evicted.
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]  # Old, resolved.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review({"relevant_file": "x.py", "issue_header": "Race",
                                      "issue_content": "Two saves interleave."}))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")
        self.assertEqual(self.gate(github, head=B)[0], 1)

    def test_reintroduction_is_recognized_after_its_pruned_record_is_gone(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        state = github.state()
        record = state["findings"].pop()
        # Fixed, pruned, its threads resolved, and the pruned record dropped by a later checkpoint.
        state["fixed_ids"], state["pruned"] = [record["id"]], []
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review({"relevant_file": "x.py", "issue_header": "Race",
                                      "issue_content": "Two saves interleave."}))
        self.assertEqual(self.gate(github, head=B)[0], 1)

    def test_gate_rejects_a_checkpoint_for_another_base_branch(self):
        github = self.reviewed()
        github.base_ref = "release"
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("No PR-Agent checkpoint", output)

    def test_pruned_renamed_finding_still_resolves_its_original_thread(self):
        finding = {"id": "1", "path": "moved.py", "thread_path": "x.py", "header": "Race", "content": "c" * 500,
                   "line": 1, "state": "FIXED", "fingerprint": cp.inline_fingerprint("x.py", "Race", "c" * 500),
                   "checked_sha": B, "check": {"verdict": "fixed", "reason": "r", "sha": B}}
        state = {"head_sha": B, "findings": [finding], "security": {}, "runs": []}
        saved, cp.COMMENT_LIMIT = cp.COMMENT_LIMIT, 800
        try:
            cp.fit_comment(state)
        finally:
            cp.COMMENT_LIMIT = saved
        self.assertEqual(state["findings"], [])
        self.assertEqual((state["pruned"][0]["id"], state["pruned"][0]["threads"]),
                         ("1", [{"path": "x.py", "fingerprint": finding["fingerprint"]}]))
        thread_on_old_path = posted("T1", "x.py", "Race", "c" * 500)
        self.assertEqual(cp.fixed_thread_ids(state, [thread_on_old_path]), ["T1"])

    def test_gate_does_not_resolve_threads_when_a_newer_checkpoint_appears(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        github.threads = [posted("T_new", "x.py", "Race", "Two saves interleave.")]
        newer = github.state()
        newer["revision"] += 1
        newer_body = cp.render_comment(newer)
        original = github.comments
        calls = {"n": 0}

        def comments(repo, pr):
            # A newer review saves between this gate's checkpoint read and its thread resolution.
            calls["n"] += 1
            if calls["n"] == 2:
                checkpoint_comment(github)["body"] = newer_body
            return original(repo, pr)
        github.comments = comments
        code, output = self.gate(github, head=B)
        self.assertEqual(github.resolved, [])
        self.assertIn("leaving thread resolution to its findings job", output)

    def test_reintroduction_at_a_renamed_path_is_recognized_after_pruning(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        state = github.state()
        record = dict(state["findings"].pop(), path="moved.py")  # Followed a rename, then fixed and pruned.
        state["fixed_ids"], state["pruned"] = [record["id"], cp.current_key(record)], []
        github.files = [{"filename": "moved.py"}, {"filename": "y.py"}]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "moved.py", "Race", "Two saves interleave.", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "moved.py"}]}
        run_review(github, B, review({"relevant_file": "moved.py", "issue_header": "Race",
                                      "issue_content": "Two saves interleave."}))
        self.assertEqual(self.gate(github, head=B)[0], 1)

    def test_threads_created_after_the_checkpoint_was_saved_are_not_resolved(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        race = finding_ids(github.state(), "x.py", "Race")[0]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(), verdicts={race: "fixed"})
        older = posted("T_old", "x.py", "Race", "Two saves interleave.")
        older["created_at"] = "2000-01-01T00:00:00Z"
        newer = posted("T_new", "x.py", "Race", "Two saves interleave.")
        newer["created_at"] = "9999-01-01T00:00:00Z"  # Posted by a newer review for a reintroduction.
        github.threads = [older, newer]
        self.gate(github, head=B)
        self.assertEqual(github.resolved, ["T_old"])

    def test_dismissed_findings_are_pruned_so_saves_keep_succeeding(self):
        paths = [f"f{i}.py" for i in range(10)]
        github = FakeGitHub("0" * 40, paths)
        previous, threads = None, []
        for round_number in range(12):
            head = f"{round_number:040d}"
            github.heads = [head]
            if previous:
                github.compares[(previous, head)] = {"status": "ahead", "files": [{"filename": p} for p in paths]}
            contents = [(p, f"round {round_number} " + "x" * 1500) for p in paths]
            # Every earlier finding was dismissed by resolving its thread; the verifier keeps saying still_present.
            still = {f["id"]: "still_present" for f in (github.state() or {}).get("findings", [])}
            code, _, _ = run_review(github, head, review(*[issue(p, "Bug", c) for p, c in contents]), verdicts=still)
            self.assertEqual(code, 0, round_number)
            threads += [current(posted(f"T{round_number}-{p}", p, "Bug", c, resolved=True)) for p, c in contents]
            github.threads = threads
            previous = head
        state = github.state()
        self.assertGreater(state["pruned_dismissed"], 0)
        self.assertEqual(self.gate(github, head=previous)[0], 0)

    def test_full_review_does_not_show_an_unchanged_files_old_patch_as_fix_evidence(self):
        github = FakeGitHub(A, ["x.py", "y.py"])
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave."), security="Injection: y.py."))
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "y.py", "patch": "+escape"}]}
        prompts = []
        _, calls, _ = run_review(github, B, review(), prompts=prompts)
        self.assertEqual(calls[0]["mode"], "full")
        x_prompt = next(p for p in prompts if p.startswith("File: x.py"))
        self.assertNotIn("@@ patch of x.py", x_prompt)
        self.assertIn("(no change to this file since then)", x_prompt)

    def test_withdrawn_dismissal_during_a_double_rename_restores_at_the_right_file(self):
        record = {"id": "1", "path": "x.py", "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint(
            "x.py", "Race", "Two saves interleave.")}]}
        github = self.reviewed()
        state = github.state()
        state["dismissed_threads"] = cp.history_entries(record)
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]  # Withdrawn.
        github.files = [{"filename": "y.py", "patch": "@@"}, {"filename": "z.py", "patch": "@@"}]
        github.contents["y.py"] = "the code formerly in x.py"
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "y.py", "previous_filename": "x.py", "status": "renamed"},
            {"filename": "z.py", "previous_filename": "y.py", "status": "renamed"}]}
        prompts = []
        run_review(github, B, review(), prompts=prompts)
        restored = github.state()["findings"][0]
        self.assertEqual(restored["path"], "y.py")
        y_prompt = next(p for p in prompts if p.startswith("File: y.py"))
        self.assertIn("the code formerly in x.py", y_prompt)

    def test_pruned_dismissal_renamed_later_restores_at_the_new_path(self):
        record = {"id": "1", "path": "x.py", "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint(
            "x.py", "Race", "Two saves interleave.")}]}
        github = self.reviewed()
        state = github.state()
        state["dismissed_threads"] = cp.history_entries(record)
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        # A push renames x.py to y.py and is saved.
        github.files = [{"filename": "y.py", "patch": "@@"}]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "y.py", "previous_filename": "x.py", "status": "renamed"}]}
        run_review(github, B, review())
        self.assertEqual(github.state()["dismissed_threads"][0]["path"], "y.py")
        # Later the dismissal is withdrawn and another push lands.
        github.threads[0]["resolved"] = False
        github.contents["y.py"] = "the renamed code"
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "y.py", "patch": "+lock"}]}
        prompts = []
        run_review(github, C, review(), prompts=prompts)
        self.assertEqual(github.state()["findings"][0]["path"], "y.py")
        self.assertTrue(prompts[0].startswith("File: y.py"))
        self.assertIn("the renamed code", prompts[0])

    def test_withdrawn_pruned_dismissal_of_a_renamed_finding_restores_at_its_new_path(self):
        dismissed = {"id": "1", "path": "moved.py", "header": "Race", "content": "Two saves interleave.",
                     "state": "DISMISSED", "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint(
                         "x.py", "Race", "Two saves interleave.")}]}
        github = self.reviewed()
        state = github.state()
        state["dismissed_threads"] = cp.history_entries(dismissed)
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.files = [{"filename": "moved.py", "patch": "@@"}]
        github.contents["moved.py"] = "the moved code"
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]  # Unresolved: withdrawn.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "moved.py", "patch": "+lock"}]}
        prompts = []
        run_review(github, B, review(), prompts=prompts)
        restored = github.state()["findings"][0]
        self.assertEqual(restored["path"], "moved.py")
        self.assertTrue(prompts[0].startswith("File: moved.py"))
        self.assertIn("the moved code", prompts[0])

    def test_history_of_many_long_path_dismissals_stays_within_its_budget(self):
        state = {"head_sha": A, "findings": [], "security": {}, "runs": [], "dismissed_threads": [], "dismissed_ids": []}
        for i in range(400):
            path = f"{'very/long/directory/name/' * 10}file{i}.py"
            moved = {"id": str(i), "path": path + ".moved", "threads": [{"path": path, "fingerprint": f"{i:012x}"}]}
            state["dismissed_threads"].extend(cp.history_entries(moved))
            state["dismissed_ids"].append(f"{i:012x}")
        body = cp.fit_comment(state)
        self.assertLessEqual(len(body), cp.COMMENT_LIMIT)
        self.assertTrue(state["history_trimmed"])

    def test_pruned_dismissal_of_a_file_with_a_newline_follows_a_rename(self):
        old, new = "src/a\nb.py", "src/c\nd.py"
        github = FakeGitHub(A, [old])
        run_review(github, A, review())
        state = github.state()
        state["dismissed_ids"] = [cp.dismissed_key({"path": old, "content_id": cp.content_id("Race", "Two saves interleave.")})]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.files = [{"filename": new, "patch": "@@"}]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": new, "previous_filename": old, "status": "renamed"}]}
        run_review(github, B, review(issue(new, "Race", "Two saves interleave.")))
        self.assertEqual(github.state()["findings"][0]["state"], "DISMISSED")

    def test_remembered_dismissal_keeps_its_original_thread(self):
        github = self.reviewed()
        state = github.state()
        state["dismissed_ids"] = [cp.dismissed_key({"path": "x.py", "content_id": cp.content_id("Race", "Two saves interleave.")})]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]  # The original thread.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(issue("x.py", "Race", "Two saves interleave.")))
        finding = github.state()["findings"][0]
        self.assertEqual(finding["state"], "DISMISSED")
        self.assertFalse(finding.get("reopened_at"))
        github.threads[0]["resolved"] = False  # Withdrawn, then resolved again: dismissed again.
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "y.py"}]}
        run_review(github, C, review())
        self.assertEqual(self.gate(github, head=C)[0], 1)
        github.threads[0]["resolved"] = True
        self.assertEqual(self.gate(github, head=C)[0], 0)

    def test_pruned_dismissal_follows_a_rename(self):
        github = self.reviewed()
        state = github.state()
        state["dismissed_ids"] = [cp.dismissed_key({"path": "x.py", "content_id": cp.content_id("Race", "Two saves interleave.")})]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.files = [{"filename": "y.py", "patch": "@@"}]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [
            {"filename": "y.py", "previous_filename": "x.py", "status": "renamed"}]}
        run_review(github, B, review(issue("y.py", "Race", "Two saves interleave.")))
        self.assertEqual(github.state()["findings"][0]["state"], "DISMISSED")
        self.assertEqual(self.gate(github, head=B)[0], 0)

    def test_pruned_renamed_dismissal_does_not_dismiss_a_new_finding_at_the_old_path(self):
        github = self.reviewed()
        state = github.state()
        moved = {"path": "moved.py", "content_id": cp.content_id("Race", "Two saves interleave.")}
        state["dismissed_ids"] = [cp.current_key(moved)]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review({"relevant_file": "x.py", "issue_header": "Race",
                                      "issue_content": "Two saves interleave."}))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")
        self.assertEqual(self.gate(github, head=B)[0], 1)

    def test_gate_does_not_resolve_a_thread_a_dismissed_finding_shares(self):
        identity = {"path": "x.py", "fingerprint": cp.inline_fingerprint("x.py", "Race", "Two saves interleave.")}
        fixed = {"id": "1", "path": "moved.py", "header": "Race", "content": "c", "state": "FIXED", "threads": [identity]}
        dismissed = {"id": "2", "path": "x.py", "header": "Race", "content": "c", "state": "DISMISSED", "threads": [identity]}
        state = {"findings": [fixed, dismissed], "saved_at": "9999-01-01T00:00:00Z"}
        thread = posted("T1", "x.py", "Race", "Two saves interleave.")  # Unresolved: the dismissal was withdrawn.
        self.assertEqual(cp.fixed_thread_ids(state, [thread]), [])
        state["findings"] = [fixed]
        state["dismissed_threads"] = [identity]
        self.assertEqual(cp.fixed_thread_ids(state, [thread]), [])

    def test_restored_dismissal_in_a_review_run_sees_the_whole_pr(self):
        github = self.reviewed()
        state = github.state()
        record = {"id": "1", "path": "x.py", "content_id": "c", "threads": [{"path": "x.py", "fingerprint":
                  cp.inline_fingerprint("x.py", "Race", "Two saves interleave.")}]}
        state["dismissed_threads"] = cp.history_entries(record)
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "helper.py", "patch": "+lock helper"}]}
        run_review(github, B, review())  # The fix (in helper.py) is saved while the finding is still dismissed.
        github.threads[0]["resolved"] = False  # Withdrawn later.
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "y.py", "patch": "+unrelated"}]}
        github.compares[(github.merge_base, C)] = {"status": "ahead", "files": [
            {"filename": "helper.py", "patch": "+lock helper"}, {"filename": "y.py", "patch": "+unrelated"}]}
        prompts = []
        run_review(github, C, review(), prompts=prompts)  # A review run: y.py changed.
        race_prompt = next(p for p in prompts if "Two saves interleave." in p)
        self.assertIn("+lock helper", race_prompt)

    def test_pruned_dismissal_withdrawn_after_the_fixing_push_is_verified_on_a_rerun(self):
        github = self.reviewed()
        state = github.state()
        record = {"id": "1", "path": "x.py", "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint(
            "x.py", "Race", "Two saves interleave.")}]}
        state["dismissed_threads"] = cp.history_entries(record)
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, B, review())  # The fixing push, saved while still dismissed.
        github.threads[0]["resolved"] = False  # Withdrawn after the save.
        restored_id = cp.finding_id("x.py", "Race", "Two saves interleave.")
        prompts = []
        code, calls, _ = run_review(github, B, review(), verdicts={restored_id: "fixed"}, prompts=prompts)
        self.assertEqual((code, calls), (0, []))
        self.assertIn("@@ patch of x.py", prompts[0])  # Verified against its whole PR patch.
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_dismissal_withdrawn_after_the_fixing_push_is_verified_on_a_rerun(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        github.threads = [current(posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True))]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, B, review())  # Still dismissed, so the fix is not verified.
        race = github.state()["findings"][0]
        self.assertEqual(race["state"], "DISMISSED")
        github.threads[0]["resolved"] = False  # Withdrawn after the save.
        code, calls, _ = run_review(github, B, review(), verdicts={race["id"]: "fixed"})  # Re-run at the same head.
        self.assertEqual((code, calls), (0, []))
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_withdrawn_dismissal_is_verified_by_the_push_that_fixes_it(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        github.threads = [current(posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True))]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "y.py"}]}
        run_review(github, B, review())
        race = github.state()["findings"][0]
        self.assertEqual(race["state"], "DISMISSED")
        github.threads[0]["resolved"] = False  # The dismissal is withdrawn before the fix is pushed.
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        prompts = []
        run_review(github, C, review(), verdicts={race["id"]: "fixed"}, prompts=prompts)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")

    def test_restored_dismissal_and_an_active_finding_in_one_file_verify_together(self):
        github = self.reviewed(issue("x.py", "Leak", "The handle is never closed."))
        state = github.state()
        record = {"id": "1", "path": "x.py", "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint(
            "x.py", "Race", "Two saves interleave.")}]}
        state["dismissed_threads"] = cp.history_entries(record)
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]  # Withdrawn.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+close()"}]}
        code, _, _ = run_review(github, B, review())  # Batches with and without a check commit sort fine.
        self.assertEqual(code, 0)
        self.assertEqual(sorted(f["header"] for f in github.state()["findings"]), ["Leak", "Race"])

    def test_restored_dismissal_is_verified_once_on_a_no_review_push(self):
        github = self.reviewed()
        state = github.state()
        record = {"id": "1", "path": "x.py", "content_id": "c", "threads": [{"path": "x.py", "fingerprint":
                  cp.inline_fingerprint("x.py", "Race", "Two saves interleave.")}]}
        state["dismissed_threads"] = cp.history_entries(record)
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]  # Withdrawn.
        github.files = [{"filename": "x.py", "patch": "@@"}]  # y.py reverted out of the PR: nothing to review.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "y.py", "patch": "-change"}]}
        prompts = []
        _, calls, _ = run_review(github, B, review(), prompts=prompts)
        self.assertEqual(calls, [])
        self.assertEqual(sum("Two saves interleave." in p for p in prompts), 1)

    def test_restoring_a_dismissal_with_a_suggestion_fence_retires_its_original_key(self):
        content = "Two saves interleave.\n```suggestion\nwith lock:\n```"
        github = self.reviewed()
        state = github.state()
        record = {"id": "1", "path": "x.py", "content_id": cp.content_id("Race", content),
                  "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint("x.py", "Race", content)}]}
        state["dismissed_threads"] = cp.history_entries(record)
        state["dismissed_ids"] = [cp.dismissed_key(record)]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", content)]  # Withdrawn; the body shows a text fence.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, B, review())
        self.assertEqual(github.state()["dismissed_ids"], [])

    def test_pruned_dismissal_withdrawn_and_then_fixed_clears_through_verification(self):
        github = self.reviewed()
        state = github.state()
        identity = {"path": "x.py", "fingerprint": cp.inline_fingerprint("x.py", "Race", "Two saves interleave.")}
        state["dismissed_threads"] = [identity]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]  # Unresolved again.
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        restored_id = cp.finding_id("x.py", "Race", "Two saves interleave.")
        prompts = []
        run_review(github, B, review(), verdicts={restored_id: "fixed"}, prompts=prompts)
        self.assertEqual(len(prompts), 1)
        self.assertIn("Two saves interleave.", prompts[0])
        restored = github.state()["findings"][0]
        self.assertEqual(restored["state"], "FIXED")
        self.assertEqual(github.state()["dismissed_threads"], [])
        code, _ = self.gate(github, head=B)
        self.assertEqual((code, github.resolved), (0, ["T1"]))

    def test_re_reported_pruned_dismissal_takes_over_its_history_and_can_be_fixed(self):
        github = self.reviewed()
        state = github.state()
        record = {"id": "old", "path": "x.py", "content_id": cp.content_id("Race", "Two saves interleave."),
                  "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint("x.py", "Race", "Two saves interleave.")}]}
        state["dismissed_threads"] = cp.history_entries(record)
        state["dismissed_ids"] = [cp.current_key(record)]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(issue("x.py", "Race", "Two saves interleave.")))  # Reported again.
        self.assertEqual(github.state()["dismissed_threads"], [])
        race = github.state()["findings"][0]
        github.threads[0]["resolved"] = False  # Withdrawn; then a fix is pushed.
        github.heads = [C]
        github.compares[(B, C)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, C, review(), verdicts={race["id"]: "fixed"})
        self.assertEqual(github.state()["findings"][0]["state"], "FIXED")
        self.assertEqual(github.state()["dismissed_ids"], [])  # The old dismissal is forgotten.
        code, _ = self.gate(github, head=C)
        self.assertEqual((code, github.resolved), (0, ["T1"]))
        # The fixed record is pruned, and the defect comes back with no inline location.
        state = github.state()
        record = state["findings"].pop()
        state["fixed_ids"] = [record["id"], cp.current_key(record)]
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads[0]["resolved"] = True
        github.heads = ["d" * 40]
        github.compares[(C, "d" * 40)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, "d" * 40, review({"relevant_file": "x.py", "issue_header": "Race",
                                             "issue_content": "Two saves interleave."}))
        self.assertEqual(github.state()["findings"][0]["state"], "ACTIVE")
        self.assertEqual(self.gate(github, head="d" * 40)[0], 1)

    def test_trimmed_flag_survives_later_saves(self):
        github = self.reviewed()
        state = github.state()
        state["history_trimmed"] = True
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "y.py"}]}
        run_review(github, B, review())  # A save that needs no trimming.
        self.assertTrue(github.state()["history_trimmed"])
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]
        self.assertEqual(self.gate(github, head=B)[0], 1)

    def test_trimmed_history_fails_closed_on_an_untracked_unresolved_thread(self):
        github = self.reviewed()
        state = github.state()
        state["history_trimmed"] = True  # Its dismissal history was evicted for size.
        checkpoint_comment(github)["body"] = cp.render_comment(state)
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("no longer tracked", output)
        # A Claude review thread (no PR-Agent footer) is not affected.
        github.threads = [thread("T2", "x.py", "**Claude**\n\nSomething.")]
        self.assertEqual(self.gate(github)[0], 0)
        # The review job rebuilds the finding, so it can be verified.
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.")]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py", "patch": "+lock"}]}
        run_review(github, B, review())
        self.assertEqual([f["header"] for f in github.state()["findings"]], ["Race"])

    def test_unresolving_a_pruned_dismissed_findings_thread_fails_the_gate(self):
        finding = {"id": "1", "path": "x.py", "header": "Race", "content": "c" * 500, "line": 1, "state": "DISMISSED",
                   "threads": [{"path": "x.py", "fingerprint": cp.inline_fingerprint("x.py", "Race", "c" * 500)}],
                   "checked_sha": A, "check": None, "content_id": "x"}
        state = {"head_sha": A, "base_ref": "main", "findings": [finding], "security": {}, "runs": []}
        saved, cp.COMMENT_LIMIT = cp.COMMENT_LIMIT, 800
        try:
            body = cp.fit_comment(state)
        finally:
            cp.COMMENT_LIMIT = saved
        self.assertEqual(state["findings"], [])
        github = FakeGitHub(A, ["x.py"])
        github.issue_comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": body,
                                  "html_url": "https://example/checkpoint"}]
        github.threads = [posted("T1", "x.py", "Race", "c" * 500, resolved=True)]
        self.assertEqual(self.gate(github)[0], 0)
        github.threads[0]["resolved"] = False
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("unresolved again", output)

    def test_dismissed_finding_stays_dismissed_when_raised_again_and_reopens_when_unresolved(self):
        github = self.reviewed(issue("x.py", "Race", "Two saves interleave."))
        github.threads = [current(posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True))]
        github.heads = [B]
        github.compares[(A, B)] = {"status": "ahead", "files": [{"filename": "x.py"}]}
        run_review(github, B, review(issue("x.py", "Race", "Two saves interleave.")))
        self.assertEqual(github.state()["findings"][0]["state"], "DISMISSED")
        self.assertEqual(self.gate(github, head=B)[0], 0)
        github.threads[0]["resolved"] = False  # Someone unresolves it: the dismissal is withdrawn.
        self.assertEqual(self.gate(github, head=B)[0], 1)

    def test_marker_quoted_inside_an_unrelated_bot_thread_does_not_dismiss(self):
        github = self.reviewed({"relevant_file": "x.py", "issue_header": "Design", "issue_content": "Retries duplicate."})
        design = github.state()["findings"][0]
        marker = f"<!-- pr-agent-dedup: {design['threads'][0]['fingerprint']} -->"
        github.threads = [thread("T_claude", "x.py", f"**Claude**\n\nAs PR-Agent's {marker} said, retries duplicate.\n\nDone.",
                                 resolved=True)]
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("Design", output)

    def test_marker_quoted_in_another_threads_text_does_not_dismiss(self):
        github = self.reviewed({"relevant_file": "x.py", "issue_header": "Design", "issue_content": "Retries duplicate."},
                               issue("x.py", "Race", "Two saves interleave."))
        design = next(f for f in github.state()["findings"] if f["header"] == "Design")
        quoted = f"<!-- pr-agent-dedup: {design['threads'][0]['fingerprint']} -->"
        race_thread = posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)
        race_thread["body"] = race_thread["body"].replace("**Race**", f"**Race** (see {quoted})", 1)
        github.threads = [race_thread]
        code, output = self.gate(github)
        self.assertEqual(code, 1)
        self.assertIn("Design", output)

    def test_a_thread_shared_by_two_findings_dismisses_neither(self):
        identity = {"path": "x.py", "fingerprint": cp.inline_fingerprint("x.py", "Race", "Two saves interleave.")}
        moved = {"id": "1", "path": "moved.py", "header": "Race", "content": "Two saves interleave.",
                 "state": "ACTIVE", "threads": [identity], "line": None}
        recreated = {"id": "2", "path": "x.py", "header": "Race", "content": "Two saves interleave.",
                     "state": "ACTIVE", "threads": [identity], "line": 3}
        state = {"findings": [moved, recreated]}
        thread_for_new = current(posted("T_new", "x.py", "Race", "Two saves interleave.", resolved=True))
        lines = cp.open_findings(state, [thread_for_new], "https://example/checkpoint")
        self.assertEqual(len(lines), 2)

    def test_thread_from_a_failed_first_attempt_dismisses_after_a_retry(self):
        github = FakeGitHub(A, ["x.py"])
        with self.assertRaises(cp.RunFailed):
            run_review(github, A, cp.RunFailed("PR-Agent exited with code 1."))  # Posted its thread, then failed.
        github.threads = [posted("T1", "x.py", "Race", "Two saves interleave.", resolved=True)]  # From that attempt.
        run_review(github, A, review(issue("x.py", "Race", "Two saves interleave.")))  # The retry, deduplicated.
        self.assertEqual(self.gate(github)[0], 0)

    def test_security_concern_blocks(self):
        code, output = self.gate(self.reviewed(security="Secrets in logs: the token is printed."))
        self.assertEqual(code, 1)
        self.assertIn("Security concern", output)


if __name__ == "__main__":
    unittest.main()
