#!/usr/bin/env python3
"""Fixture tests of the "/gpu-tests" decision in gpu-request.py.

Each case is an issue_comment event plus the API answers; the test prints the expected
and the actual decision and fails if any case differs.
"""
import copy
import datetime
import importlib.util
import os
import sys
import urllib.parse

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "gpu_request", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu-request.py"))
gpu_request = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu_request)

NOW = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.timezone.utc)
BOT = "arrayfire-claude[bot]"
HEAD = "cd211d5b8660761d1665be52ace5ff680856ba06"
# An earlier head of the pull request, green on every check; it must not count.
STALE_HEAD = "0" * 40
PR = 3715
SETTINGS = {"gate": "true", "bot": BOT, "per_pr_day": 2, "per_day": 6, "run_id": "900"}


def event(login, body="/gpu-tests", user_type="User", pull_request=True):
    issue = {"number": PR, "title": "Add support for CUDA 13.1"}
    if pull_request:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/arrayfire/arrayfire/pulls/{PR}"}
    return {"action": "created", "issue": issue,
            "comment": {"body": body, "user": {"login": login, "type": user_type}}}


def run(run_id, hours_ago, pr, accepted=True):
    created = (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ({"id": run_id, "actor": {"login": BOT}, "created_at": created,
             "display_title": f"/gpu-tests on #{pr} by {BOT}"},
            [{"name": "decide", "conclusion": "success"},
             {"name": "accepted", "conclusion": "success" if accepted else "skipped"}])


def comment_run(run_id, pr):
    # A run for an ordinary comment; even a stray "accepted" job in it must not count.
    request, jobs = run(run_id, 1, pr)
    return {**request, "display_title": f"comment on #{pr} by {BOT}"}, jobs


def checks(**conclusions):
    names = ("lint", "docs", "linux-gcc-openblas", "linux-clang-openblas")
    return [{"id": 10 + i, "name": name, "conclusion": conclusions.get(name.replace("-", "_"), "success")}
            for i, name in enumerate(names)]


def answers(author="maintainer-a", check_runs=None, runs=()):
    return {"permissions": {"maintainer-a": "maintain", "admin-b": "admin", "writer-c": "write",
                            "reader-d": "read", "triager-e": "triage"},
            "pull": {"number": PR, "head": {"sha": HEAD}, "user": {"login": author}},
            "check_runs": {HEAD: checks() if check_runs is None else check_runs,
                           STALE_HEAD: checks()},
            "runs": [r for r, _ in runs], "jobs": {str(r["id"]): jobs for r, jobs in runs}}


def red_after_green():
    # An older green run of linux-clang-openblas, then a red re-run: the latest counts.
    return checks(linux_clang_openblas="failure") + [
        {"id": 5, "name": "linux-clang-openblas", "conclusion": "success"}]


CASES = [
    # name, event, answers, settings changes, expected (action, reason, sha, cuda), expected calls
    ("maintainer is accepted", event("maintainer-a"), answers(), {},
     ("accept", "accepted", HEAD, "12.9"), None),
    ("admin with cuda=13.1 is accepted", event("admin-b", "/gpu-tests cuda=13.1\nthanks"), answers(), {},
     ("accept", "accepted", HEAD, "13.1"), None),
    ("non-writer is refused", event("reader-d", user_type="User"), answers(), {},
     ("reply", "not-a-writer", "", ""), ["permission reader-d"]),
    ("triage role is refused", event("triager-e"), answers(), {},
     ("reply", "not-a-writer", "", ""), None),
    ("Claude bot is accepted when eligible", event(BOT, user_type="Bot"),
     answers(runs=[run(101, 3, PR), run(102, 5, 3700), run(103, 8, 3701), run(104, 1, PR, accepted=False)]),
     {}, ("accept", "accepted", HEAD, "12.9"), None),
    ("Claude bot is accepted on its own PR", event(BOT, user_type="Bot"), answers(author=BOT), {},
     ("accept", "accepted", HEAD, "12.9"), None),
    ("Claude bot is refused when CPU CI is red", event(BOT, user_type="Bot"),
     answers(check_runs=red_after_green()), {}, ("reply", "cpu-not-green", "", ""), None),
    ("Claude bot is refused when a CPU check is missing", event(BOT, user_type="Bot"),
     answers(check_runs=[c for c in checks() if c["name"] != "docs"]), {},
     ("reply", "cpu-not-green", "", ""), None),
    ("Claude bot is refused over the per-PR quota", event(BOT, user_type="Bot"),
     answers(runs=[run(101, 2, PR), run(102, 20, PR)]), {}, ("reply", "pr-quota", "", ""), None),
    ("per-PR quota counts only the last 24 h", event(BOT, user_type="Bot"),
     answers(runs=[run(101, 2, PR), run(102, 25, PR)]), {}, ("accept", "accepted", HEAD, "12.9"), None),
    ("plain comments by the bot are not requests", event(BOT, user_type="Bot"),
     answers(runs=[comment_run(200 + i, PR) for i in range(5)]), {}, ("accept", "accepted", HEAD, "12.9"), None),
    ("Claude bot is refused over the daily quota", event(BOT, user_type="Bot"),
     answers(runs=[run(100 + i, i + 1, 3000 + i) for i in range(6)]), {},
     ("reply", "daily-quota", "", ""), None),
    ("daily quota follows AF_CI_GPU_AI_PER_DAY", event(BOT, user_type="Bot"),
     answers(runs=[run(100 + i, i + 1, 3000 + i) for i in range(6)]), {"per_day": 10},
     ("accept", "accepted", HEAD, "12.9"), None),
    ("Claude bot is refused on an external contributor's PR", event(BOT, user_type="Bot"),
     answers(author="outsider-f"), {}, ("reply", "external-author", "", ""), None),
    ("a user named like the bot is not the bot", event("arrayfire-claude"), answers(), {},
     ("reply", "not-a-writer", "", ""), None),
    ("gate off answers disabled without any API call", event("maintainer-a"), answers(),
     {"gate": ""}, ("reply", "disabled", "", ""), []),
    ("malformed cuda argument gets the usage", event("maintainer-a", "/gpu-tests cuda=thirteen"),
     answers(), {}, ("reply", "usage", "", ""), []),
    ("unknown argument gets the usage", event("maintainer-a", "/gpu-tests now please"), answers(), {},
     ("reply", "usage", "", ""), []),
    ("github-actions[bot] comment is ignored", event("github-actions[bot]", user_type="Bot"),
     answers(), {}, ("ignore", "own-comment", "", ""), []),
    ("comment on an issue is ignored", event("maintainer-a", pull_request=False), answers(), {},
     ("ignore", "not-pull-request", "", ""), []),
    ("command not on the first line is ignored", event("maintainer-a", "please run\n/gpu-tests"),
     answers(), {}, ("ignore", "not-a-request", "", ""), []),
    ("a longer word is not the command", event("maintainer-a", "/gpu-testsuite"), answers(), {},
     ("ignore", "not-a-request", "", ""), []),
]


def run_listing():
    """GitHub.request_runs asks for the bot's runs only and follows every page."""
    class Paged(gpu_request.GitHub):
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


def main():
    failed = 0 if run_listing() else 1
    for name, ev, api_answers, changes, expected, expected_calls in CASES:
        api = gpu_request.Fixtures(copy.deepcopy(api_answers))
        result = gpu_request.decide(ev, {**SETTINGS, **changes}, api, NOW)
        actual = (result["action"], result["reason"], result["sha"], result["cuda"])
        ok = actual == expected
        if result["action"] == "accept":
            ok = ok and result["sha"] in result["reply"]
        if expected_calls is not None:
            ok = ok and api.calls == expected_calls
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}\n"
              f"     expected: {' '.join(e or '-' for e in expected)}\n"
              f"     actual:   {' '.join(a or '-' for a in actual)}"
              + (f"\n     API calls: {api.calls or 'none'}" if expected_calls is not None else "")
              + (f"\n     reply: {result['reply']}" if result["reply"] else ""))
    print(f"{len(CASES) + 1 - failed} of {len(CASES) + 1} cases match")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
