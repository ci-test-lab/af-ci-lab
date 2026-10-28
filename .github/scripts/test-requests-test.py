#!/usr/bin/env python3
"""Fixture tests of the "/cpu-tests" and "/gpu-tests" decisions in test-requests.py, and of
how its commands handle failed API calls.

Each decision case is an issue_comment event plus the API answers; the test prints the
expected and the actual decision and fails if any case differs. The command cases run
test-requests.py as the workflow's jobs do, on fixtures, with some writes failing.
"""
import contextlib
import copy
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-requests.py")
spec = importlib.util.spec_from_file_location("test_requests", SCRIPT)
test_requests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(test_requests)

NOW = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.timezone.utc)
BOT = "arrayfire-claude[bot]"
HEAD = "cd211d5b8660761d1665be52ace5ff680856ba06"
# An earlier head of the pull request, green on every check; it must not count.
STALE_HEAD = "0" * 40
PR = 3715
SETTINGS = {"gpu_tests": "true", "gpu_runner": "gpu-t4", "accelerator_builds": "true", "bot": BOT,
            "per_pr_day": 2, "per_day": 6, "run_id": "900"}
ASSOCIATION = {"maintainer-a": "MEMBER", "admin-b": "OWNER", "writer-c": "COLLABORATOR",
               "reader-d": "COLLABORATOR", "triager-e": "COLLABORATOR", BOT: "NONE",
               "contributor-g": "CONTRIBUTOR", "github-actions[bot]": "NONE"}


def event(login, body="/gpu-tests", author="maintainer-a", association=None, pull_request=True):
    issue = {"number": PR, "title": "Add support for CUDA 13.1", "user": {"login": author}}
    if pull_request:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/arrayfire/arrayfire/pulls/{PR}"}
    return {"action": "created", "issue": issue,
            "comment": {"body": body, "user": {"login": login},
                        "author_association": association or ASSOCIATION.get(login, "NONE")}}


def run(run_id, hours_ago, pr, accepted=True, command="/gpu-tests"):
    created = (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ({"id": run_id, "actor": {"login": BOT}, "created_at": created,
             "display_title": f"{command} on #{pr} by {BOT}"},
            [{"name": "decide", "conclusion": "success"},
             {"name": "accepted", "conclusion": "success" if accepted else "skipped"}])


def comment_run(run_id, pr):
    # A run for an ordinary comment; even a stray "accepted" job in it must not count.
    request, jobs = run(run_id, 1, pr)
    return {**request, "display_title": f"comment on #{pr} by {BOT}"}, jobs


def checks(app="github-actions", **conclusions):
    """The light checks on a commit; a conclusion of None means still running."""
    names = ("lint", "docs", "workflows")
    return [{"id": 10 + i, "name": name, "app": {"slug": app},
             "conclusion": conclusions.get(name, "success")} for i, name in enumerate(names)]


def status(state, creator="github-actions[bot]", context="cpu-tests"):
    return {"context": context, "state": state, "creator": {"login": creator}}


def answers(author="maintainer-a", check_runs=None, statuses=None, runs=()):
    """API answers; statuses are newest first, and by default CPU tests passed on HEAD."""
    return {"permissions": {"maintainer-a": "maintain", "admin-b": "admin", "writer-c": "write",
                            "reader-d": "read", "triager-e": "triage"},
            "pull": {"number": PR, "head": {"sha": HEAD}, "user": {"login": author}},
            "check_runs": {HEAD: checks() if check_runs is None else check_runs,
                           STALE_HEAD: checks()},
            "statuses": {HEAD: [status("success"), status("pending")] if statuses is None else statuses,
                         STALE_HEAD: [status("success")]},
            "runs": [r for r, _ in runs], "jobs": {str(r["id"]): jobs for r, jobs in runs}}


def red_after_green():
    # An older green run of lint, then a red re-run: the latest counts.
    return checks(lint="failure") + [
        {"id": 5, "name": "lint", "app": {"slug": "github-actions"}, "conclusion": "success"}]


def accept(command, cuda="", accelerators=False, macos_windows=False):
    return ("accept", "accepted", command, HEAD, cuda, accelerators, macos_windows)


def refuse(command, reason):
    return ("reply", reason, command, "", "", False, False)


def ignore(reason, command=""):
    return ("ignore", reason, command, "", "", False, False)


CASES = [
    # name, event, answers, settings changes, expected, expected calls
    # /cpu-tests: writers
    ("cpu: maintainer is accepted, with macOS and Windows", event("maintainer-a", "/cpu-tests"),
     answers(), {}, accept("cpu", macos_windows=True), ["permission maintainer-a", f"pull {PR}"]),
    ("cpu: writer's /cpu-tests all adds the accelerator builds", event("writer-c", "/cpu-tests all"),
     answers(), {}, accept("cpu", accelerators=True, macos_windows=True), None),
    ("cpu: /cpu-tests all says when accelerator builds are disabled", event("writer-c", "/cpu-tests all"),
     answers(), {"accelerator_builds": ""}, accept("cpu", accelerators=True, macos_windows=True), None),
    # /cpu-tests: the pull request's own author without write access
    ("cpu: author without write access, light checks done, is accepted on Linux",
     event("contributor-g", "/cpu-tests", author="contributor-g"), answers(author="contributor-g"), {},
     accept("cpu"), ["permission contributor-g", f"pull {PR}", f"check-runs {HEAD}"]),
    ("cpu: first-time author whose light checks never ran is refused",
     event("newcomer-h", "/cpu-tests", author="newcomer-h", association="FIRST_TIME_CONTRIBUTOR"),
     answers(author="newcomer-h", check_runs=[]), {}, refuse("cpu", "light-checks-pending"), None),
    ("cpu: author is refused while a light check is still running",
     event("contributor-g", "/cpu-tests", author="contributor-g"),
     answers(author="contributor-g", check_runs=checks(docs=None)), {},
     refuse("cpu", "light-checks-pending"), None),
    ("cpu: light checks from another app don't count",
     event("contributor-g", "/cpu-tests", author="contributor-g"),
     answers(author="contributor-g", check_runs=checks(app="some-other-app")), {},
     refuse("cpu", "light-checks-pending"), None),
    ("cpu: someone else's pull request is not the author's", event("reader-d", "/cpu-tests"),
     answers(), {}, refuse("cpu", "not-allowed"), None),
    # /cpu-tests: the Claude bot
    ("cpu: Claude bot is accepted on Linux", event(BOT, "/cpu-tests"), answers(check_runs=[]), {},
     accept("cpu"), [f"pull {PR}"]),
    # /cpu-tests: anyone else
    ("cpu: stranger is ignored silently", event("contributor-g", "/cpu-tests"), answers(), {},
     ignore("stranger", "cpu"), []),
    ("cpu: stranger's malformed request gets no usage reply", event("contributor-g", "/cpu-tests now"),
     answers(), {}, ignore("stranger", "cpu"), []),
    ("cpu: malformed request gets the usage", event("maintainer-a", "/cpu-tests everything"),
     answers(), {}, refuse("cpu", "usage"), []),

    # /gpu-tests: writers
    ("gpu: maintainer is accepted", event("maintainer-a"), answers(), {}, accept("gpu", "12.9"), None),
    ("gpu: admin with cuda=13.1 is accepted", event("admin-b", "/gpu-tests cuda=13.1\nthanks"),
     answers(), {}, accept("gpu", "13.1"), None),
    # /gpu-tests: the author without write access, and other non-writers
    ("gpu: author without write access is refused",
     event("contributor-g", author="contributor-g"), answers(author="contributor-g"), {},
     refuse("gpu", "not-allowed"), ["permission contributor-g"]),
    ("gpu: non-writer is refused", event("reader-d"), answers(), {}, refuse("gpu", "not-allowed"),
     ["permission reader-d"]),
    ("gpu: triage role is refused", event("triager-e"), answers(), {}, refuse("gpu", "not-allowed"), None),
    ("gpu: stranger is ignored silently", event("contributor-g"), answers(), {},
     ignore("stranger", "gpu"), []),
    # /gpu-tests: the Claude bot
    ("gpu: Claude bot is accepted when eligible", event(BOT),
     answers(runs=[run(101, 3, PR), run(102, 5, 3700), run(103, 8, 3701), run(104, 1, PR, accepted=False)]),
     {}, accept("gpu", "12.9"), None),
    ("gpu: Claude bot is accepted on its own PR", event(BOT, author=BOT), answers(author=BOT), {},
     accept("gpu", "12.9"), None),
    ("gpu: Claude bot is refused when its CPU tests are red", event(BOT),
     answers(statuses=[status("failure"), status("success"), status("pending")]), {},
     refuse("gpu", "cpu-not-green"), None),
    ("gpu: Claude bot is refused when its CPU tests never ran", event(BOT),
     answers(statuses=[status("success", context="other")]), {}, refuse("gpu", "cpu-not-green"), None),
    ("gpu: a cpu-tests status posted by the bot itself doesn't count", event(BOT),
     answers(statuses=[status("success", creator=BOT), status("failure")]), {},
     refuse("gpu", "cpu-not-green"), None),
    ("gpu: Claude bot is refused when a light check is red", event(BOT),
     answers(check_runs=red_after_green()), {}, refuse("gpu", "cpu-not-green"), None),
    ("gpu: Claude bot is refused when a light check is missing", event(BOT),
     answers(check_runs=[c for c in checks() if c["name"] != "docs"]), {},
     refuse("gpu", "cpu-not-green"), None),
    ("gpu: Claude bot is refused over the per-PR quota", event(BOT),
     answers(runs=[run(101, 2, PR), run(102, 20, PR)]), {}, refuse("gpu", "pr-quota"), None),
    ("gpu: per-PR quota counts only the last 24 h", event(BOT),
     answers(runs=[run(101, 2, PR), run(102, 25, PR)]), {}, accept("gpu", "12.9"), None),
    ("gpu: plain comments by the bot are not requests", event(BOT),
     answers(runs=[comment_run(200 + i, PR) for i in range(5)]), {}, accept("gpu", "12.9"), None),
    ("gpu: CPU requests don't count against the GPU quota", event(BOT),
     answers(runs=[run(300 + i, 1, PR, command="/cpu-tests") for i in range(5)]), {},
     accept("gpu", "12.9"), None),
    ("gpu: Claude bot is refused over the daily quota", event(BOT),
     answers(runs=[run(100 + i, i + 1, 3000 + i) for i in range(6)]), {}, refuse("gpu", "daily-quota"),
     None),
    ("gpu: daily quota follows AF_CI_GPU_AI_PER_DAY", event(BOT),
     answers(runs=[run(100 + i, i + 1, 3000 + i) for i in range(6)]), {"per_day": 10},
     accept("gpu", "12.9"), None),
    ("gpu: Claude bot is refused on an external contributor's PR", event(BOT),
     answers(author="outsider-f"), {}, refuse("gpu", "external-author"), None),
    ("gpu: a user named like the bot is not the bot", event("arrayfire-claude"), answers(), {},
     ignore("stranger", "gpu"), []),
    # /gpu-tests: settings and syntax, all answered without an API call
    ("gpu: gate off answers disabled without any API call", event("maintainer-a"), answers(),
     {"gpu_tests": ""}, refuse("gpu", "disabled"), []),
    ("gpu: no GPU runner configured is refused up front", event("maintainer-a"), answers(),
     {"gpu_runner": ""}, refuse("gpu", "no-runner"), []),
    ("gpu: malformed cuda argument gets the usage", event("maintainer-a", "/gpu-tests cuda=thirteen"),
     answers(), {}, refuse("gpu", "usage"), []),
    ("gpu: unknown argument gets the usage", event("maintainer-a", "/gpu-tests now please"), answers(),
     {}, refuse("gpu", "usage"), []),

    # Comments that are not requests
    ("github-actions[bot] comment is ignored", event("github-actions[bot]"), answers(), {},
     ignore("own-comment"), []),
    ("comment on an issue is ignored", event("maintainer-a", pull_request=False), answers(), {},
     ignore("not-pull-request"), []),
    ("command not on the first line is ignored", event("maintainer-a", "please run\n/gpu-tests"),
     answers(), {}, ignore("not-a-request"), []),
    ("a longer word is not the command", event("maintainer-a", "/gpu-testsuite"), answers(), {},
     ignore("not-a-request"), []),
]


def run_listing():
    """GitHub.request_runs asks for the bot's runs only and follows every page."""
    class Paged(test_requests.GitHub):
        def __init__(self):
            super().__init__("arrayfire/arrayfire", "")
            self.paths = []

        def call(self, method, path, body=None):
            self.paths.append(path)
            if len(self.paths) > 5:
                raise RuntimeError("the run listing does not stop paging")
            page = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page"][0]
            return {"workflow_runs": [{"id": i} for i in range(100 if page == "1" else 7)]}

    api = Paged()
    runs = api.request_runs(BOT, NOW - datetime.timedelta(hours=24))
    actual = (len(runs), len(api.paths), "actor=arrayfire-claude%5Bbot%5D" in api.paths[0],
              "created=%3E%3D2026-10-24T12%3A00%3A00Z" in api.paths[0])
    expected = (107, 2, True, True)
    print(f"{'ok  ' if actual == expected else 'FAIL'} run listing filters by actor and reads every page\n"
          f"     expected: runs, requests, actor filter, 24 h window = {expected}\n"
          f"     actual:   runs, requests, actor filter, 24 h window = {actual}")
    return actual == expected


def api_error_annotation():
    """GitHub.call turns an HTTP error into an annotation with the method, the path, the
    status and GitHub's message, cut to about 200 characters, and re-raises it; the request's
    headers, which hold the token, are not in it."""
    token = "test-token-not-a-credential"
    sent = []

    def urlopen(request, timeout):
        sent.append(request)
        body = {"message": "Resource not accessible by integration " + "x" * 300,
                "documentation_url": "https://docs.github.com/rest/issues/comments"}
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {"X-GitHub-Request-Id": "1"},
                                     io.BytesIO(json.dumps(body).encode()))

    output = io.StringIO()
    with unittest.mock.patch.object(urllib.request, "urlopen", urlopen), \
            contextlib.redirect_stdout(output):
        try:
            test_requests.GitHub("arrayfire/arrayfire", token).comment(PR, "CPU tests requested.")
            raised = None
        except urllib.error.HTTPError as error:
            raised = error.code
    printed = output.getvalue()
    prefix = f"::error::GitHub API POST issues/{PR}/comments failed: HTTP 403: "
    message = printed.strip()[len(prefix):]
    actual = (raised, sent[0].get_header("Authorization") == f"Bearer {token}",
              printed.startswith(prefix) and printed.count("\n") == 1,
              message.startswith("Resource not accessible by integration") and len(message) <= 200,
              "Authorization" in printed or "Bearer" in printed or token in printed)
    expected = (403, True, True, True, False)
    print(f"{'ok  ' if actual == expected else 'FAIL'} an API error is annotated without the request's headers\n"
          f"     expected: re-raised, token sent, one annotation, message cut to 200, headers shown = {expected}\n"
          f"     actual:   re-raised, token sent, one annotation, message cut to 200, headers shown = {actual}\n"
          f"     printed: {printed.strip()}")
    return actual == expected


def permission_not_found():
    """permission() still reads a 404 as no access, without an annotation."""
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {},
                                     io.BytesIO(b'{"message": "Not Found"}'))

    output = io.StringIO()
    with unittest.mock.patch.object(urllib.request, "urlopen", urlopen), \
            contextlib.redirect_stdout(output):
        role = test_requests.GitHub("arrayfire/arrayfire", "").permission("contributor-g")
    actual = (role, output.getvalue())
    expected = ("none", "")
    print(f"{'ok  ' if actual == expected else 'FAIL'} permission() reads a 404 as no access, quietly\n"
          f"     expected: role, output = {expected}\n"
          f"     actual:   role, output = {actual}")
    return actual == expected


def run_command(command, fail):
    """Runs test-requests.py <command> on fixtures, as the workflow's jobs run it; fail maps a
    write (comment, set_status) to the HTTP status and message it fails with."""
    fixtures = {"fail": {name: {"status": code, "body": {"message": message}}
                         for name, (code, message) in fail.items()}}
    with tempfile.TemporaryDirectory() as tmp:
        files = {}
        for name, value in (("event", event("maintainer-a", "/cpu-tests")), ("fixtures", fixtures)):
            files[name] = os.path.join(tmp, f"{name}.json")
            with open(files[name], "w") as file:
                json.dump(value, file)
        process = subprocess.run(
            [sys.executable, "-I", SCRIPT, command], capture_output=True, text=True, timeout=60,
            env={"PATH": os.environ.get("PATH", ""), "ACT": "true",
                 "AF_CI_REQUEST_FIXTURES": files["fixtures"], "GITHUB_EVENT_PATH": files["event"],
                 "GITHUB_REPOSITORY": "arrayfire/arrayfire", "GITHUB_RUN_ID": "900",
                 "COMMAND": "cpu", "SHA": HEAD, "CUDA": "", "REPLY": "CPU tests requested.",
                 "RESULT": "success", "SUMMARY": ""})
    return process.returncode, process.stdout + process.stderr


FORBIDDEN = (403, "Resource not accessible by integration")
COMMENT_FAILED = f"GitHub API POST issues/{PR}/comments failed: HTTP 403: Resource not accessible by integration"
STATUS_FAILED = f"GitHub API POST statuses/{HEAD} failed: HTTP 403: Resource not accessible by integration"
COMMAND_CASES = [
    # name, command, failing writes, exits with 0, printed, not printed
    ("accepted: sets the pending status and comments", "accepted", {}, True,
     [f"[fixture] status cpu-tests=pending on {HEAD}", f"[fixture] comment on #{PR}"], ["::"]),
    ("accepted: a failed comment is a warning; the status stays and the tests go on", "accepted",
     {"comment": FORBIDDEN}, True,
     [f"[fixture] status cpu-tests=pending on {HEAD}", f"::warning::{COMMENT_FAILED}"],
     ["[fixture] comment", "::error::"]),
    ("accepted: a failed status fails the job", "accepted", {"set_status": FORBIDDEN}, False,
     [f"::error::{STATUS_FAILED}"], ["[fixture] status", "[fixture] comment"]),
    ("report: a failed comment is a warning; the status stays", "report", {"comment": FORBIDDEN}, True,
     [f"[fixture] status cpu-tests=success on {HEAD}", f"::warning::{COMMENT_FAILED}"],
     ["[fixture] comment", "::error::"]),
    ("report: a failed status fails the job", "report", {"set_status": FORBIDDEN}, False,
     [f"::error::{STATUS_FAILED}"], ["[fixture] status", "[fixture] comment"]),
    ("reply: comments", "reply", {}, True, [f"[fixture] comment on #{PR}:\nCPU tests requested."], ["::"]),
    ("reply: a failed comment fails the job", "reply", {"comment": FORBIDDEN}, False,
     [f"::error::{COMMENT_FAILED}"], ["[fixture] comment", "::warning::"]),
]


def command_case(name, command, fail, succeeds, printed, not_printed):
    code, output = run_command(command, fail)
    ok = ((code == 0) == succeeds and all(text in output for text in printed)
          and not any(text in output for text in not_printed))
    shown = [line for line in output.splitlines() if line.startswith(("::", "[fixture]", "Not commenting"))]
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n"
          f"     expected: exit {'0' if succeeds else 'non-zero'}\n"
          f"     actual:   exit {code}"
          + "".join(f"\n     printed: {line}" for line in shown))
    return ok


def show(values):
    action, reason, command, sha, cuda, accelerators, macos_windows = values
    return (f"{action} {reason} command={command or '-'} sha={sha or '-'} cuda={cuda or '-'} "
            f"accelerators={str(accelerators).lower()} macos-windows={str(macos_windows).lower()}")


def main():
    results = [run_listing()]
    for name, ev, api_answers, changes, expected, expected_calls in CASES:
        api = test_requests.Fixtures(copy.deepcopy(api_answers))
        result = test_requests.decide(ev, {**SETTINGS, **changes}, api, NOW)
        actual = (result["action"], result["reason"], result["command"], result["sha"], result["cuda"],
                  result["accelerators"], result["macos_windows"])
        ok = actual == expected
        if result["action"] == "accept":
            ok = ok and result["sha"] in result["reply"]
        if result["action"] == "ignore":
            ok = ok and not result["reply"]
        if expected_calls is not None:
            ok = ok and api.calls == expected_calls
        results.append(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {name}\n"
              f"     expected: {show(expected)}\n"
              f"     actual:   {show(actual)}"
              + (f"\n     API calls: {api.calls or 'none'}" if expected_calls is not None else "")
              + (f"\n     reply: {result['reply']}" if result["reply"] else ""))
    results += [api_error_annotation(), permission_not_found()]
    results += [command_case(*case) for case in COMMAND_CASES]
    print(f"{sum(results)} of {len(results)} cases match")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
