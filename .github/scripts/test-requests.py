#!/usr/bin/env python3
"""Handles "/cpu-tests" and "/gpu-tests" requests on pull requests for test-requests.yml.

  test-requests.py decide     decide on the issue_comment event in GITHUB_EVENT_PATH and
                              write decision, command, sha, cuda, accelerators,
                              macos-windows and reply to GITHUB_OUTPUT
  test-requests.py reply      post REPLY on the pull request
  test-requests.py accepted   set the pending COMMAND-tests status on SHA and post REPLY
  test-requests.py report     set the final status from RESULT and SUMMARY (the called
                              workflow's result and, for GPU runs, its test summary)
                              and comment

The commit status is the result; in accepted and report a comment that fails is only a
warning. A failed API call becomes an annotation of the run, which, unlike the job log,
can be read without logging in.

decide() depends only on the event, the settings and the API answers. With
AF_CI_REQUEST_FIXTURES naming a JSON file of API answers, those replace GitHub and
nothing is written; that is honoured only in act runs (ACT=true).
"""
import datetime
import io
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

COMMANDS = {"cpu": re.compile(r"/cpu-tests(?:\s+(all))?"),
            "gpu": re.compile(r"/gpu-tests(?:\s+cuda=(\d+\.\d+))?")}
USAGE = {"cpu": "Usage: `/cpu-tests`, or `/cpu-tests all` to add the CUDA, OpenCL and oneAPI builds.",
         "gpu": "Usage: `/gpu-tests` or `/gpu-tests cuda=<major.minor>`, for example `/gpu-tests cuda=13.1`."}
DEFAULT_CUDA = "12.9"  # gpu.yml's default
LIGHT_CHECKS = ("lint", "docs", "workflows")  # checks.yml, run on every push
WRITE_ROLES = {"admin", "maintain", "write"}
# Commenters who get an answer besides the pull request's author; others are
# ignored, so that nobody can make the workflow spam a pull request.
KNOWN = {"OWNER", "MEMBER", "COLLABORATOR"}
# The account of GitHub Actions' own token: its comments are ignored, and only its
# cpu-tests statuses, which this workflow posts, count.
ACTIONS_BOT = "github-actions[bot]"


def decision(action, reason, reply="", command="", sha="", cuda="", accelerators=False,
             macos_windows=False):
    return {"action": action, "reason": reason, "reply": reply, "command": command, "sha": sha,
            "cuda": cuda, "accelerators": accelerators, "macos_windows": macos_windows}


def decide(event, settings, api, now):
    """Returns the decision on one issue_comment event; asks api only what it needs."""
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    login = (comment.get("user") or {}).get("login", "")
    lines = (comment.get("body") or "").splitlines()
    first = lines[0].strip() if lines else ""

    if not issue.get("pull_request"):
        return decision("ignore", "not-pull-request")
    if login == ACTIONS_BOT:
        return decision("ignore", "own-comment")
    word = re.match(r"/(cpu|gpu)-tests(\s|$)", first)
    if not word:
        return decision("ignore", "not-a-request")
    command = word.group(1)
    bot = settings["bot"]
    is_bot = bool(bot) and login == bot
    is_author = login == (issue.get("user") or {}).get("login")
    if not (is_bot or is_author or comment.get("author_association") in KNOWN):
        return decision("ignore", "stranger", command=command)

    def reply(reason, text):
        return decision("reply", reason, text, command)

    if command == "gpu":
        if settings["gpu_tests"] != "true":
            return reply("disabled", "GPU tests are disabled in this repository.")
        if not settings["gpu_runner"]:
            return reply("no-runner", "GPU tests can't run here: no GPU runner is configured.")
    match = COMMANDS[command].fullmatch(first)
    if not match:
        return reply("usage", USAGE[command])

    writer = not is_bot and api.permission(login) in WRITE_ROLES
    if command == "gpu" and not (writer or is_bot):
        return reply("not-allowed",
                     f"Sorry @{login}, only maintainers with write access can request GPU tests.")
    if command == "cpu" and not (writer or is_bot or is_author):
        return reply("not-allowed",
                     f"Sorry @{login}, CPU tests can be requested by maintainers with write access "
                     "and by the pull request's author.")

    number = issue["number"]
    pull = api.pull(number)
    sha = pull["head"]["sha"]
    if command == "cpu":
        if not (writer or is_bot):
            checks = latest_light_checks(api, sha)
            pending = [name for name in LIGHT_CHECKS if (checks.get(name) or {}).get("conclusion") is None]
            if pending:
                return reply("light-checks-pending",
                             f"Not starting CPU tests for `{sha}` yet: the checks that run on every "
                             f"push haven't run on it ({', '.join(pending)}). A maintainer may need "
                             "to approve the workflow runs first.")
        accelerators = match.group(1) == "all"
        what = "CPU tests"
        if accelerators:
            what += (" and the accelerator builds" if settings["accelerator_builds"] == "true" else
                     " (the accelerator builds are disabled in this repository)")
        if not writer:
            what += " on Linux (macOS and Windows run in the merge queue)"
        return decision("accept", "accepted",
                        f"{what} requested by @{login} for `{sha}`, the head of this pull request "
                        "now. Later pushes are not part of this run.",
                        command, sha, accelerators=accelerators, macos_windows=writer)

    if is_bot:
        refusal = bot_refusal(settings, api, number, pull, sha, now)
        if refusal:
            reason, text = refusal
            return reply(reason, f"Not starting GPU tests for `{sha}`: {text}")
    cuda = match.group(1) or DEFAULT_CUDA
    return decision("accept", "accepted",
                    f"GPU tests (CUDA {cuda}) requested by @{login} for `{sha}`, the head of this "
                    "pull request now. The build runs first; the GPU job then waits for a "
                    "maintainer's approval. Later pushes are not part of this run.",
                    command, sha, cuda)


def latest_light_checks(api, sha):
    """The latest GitHub Actions check run of each light check on sha, by name."""
    latest = {}
    for run in api.check_runs(sha):
        if (run["name"] in LIGHT_CHECKS and (run.get("app") or {}).get("slug") == "github-actions"
                and run["id"] > latest.get(run["name"], {}).get("id", -1)):
            latest[run["name"]] = run
    return latest


def bot_refusal(settings, api, number, pull, sha, now):
    """The limits on the Claude App's own GPU requests; None when within them."""
    checks = latest_light_checks(api, sha)
    red = [name for name in LIGHT_CHECKS if (checks.get(name) or {}).get("conclusion") != "success"]
    cpu_tests = next((status["state"] for status in api.statuses(sha)
                      if status["context"] == "cpu-tests"
                      and status["creator"]["login"] == ACTIONS_BOT), None)
    if cpu_tests != "success":
        red.append("cpu-tests")
    if red:
        return "cpu-not-green", "these checks are not green on it: " + ", ".join(red) + "."

    since = now - datetime.timedelta(hours=24)
    accepted = [run for run in api.request_runs(settings["bot"], since)
                if run["actor"]["login"] == settings["bot"]
                and run["display_title"].startswith("/gpu-tests on #")
                and str(run["id"]) != settings["run_id"]
                and parse_time(run["created_at"]) >= since
                and any(job["name"] == "accepted" and job["conclusion"] != "skipped"
                        for job in api.run_jobs(run["id"]))]
    on_this_pr = [run for run in accepted if re.search(rf"#{number}\b", run["display_title"])]
    if len(on_this_pr) >= settings["per_pr_day"]:
        return "pr-quota", (f"{len(on_this_pr)} requests on this pull request in the last 24 hours "
                            f"reached the limit of {settings['per_pr_day']}.")
    if len(accepted) >= settings["per_day"]:
        return "daily-quota", (f"{len(accepted)} requests in the last 24 hours reached the "
                               f"daily limit of {settings['per_day']}.")

    author = pull["user"]["login"]
    if author != settings["bot"] and api.permission(author) not in WRITE_ROLES:
        return "external-author", f"its author @{author} has no write access."
    return None


def parse_time(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def annotate(level, text):
    """Prints text as an annotation of the run (level is error or warning)."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{text}", flush=True)


def describe(method, path, error):
    """A failed API call: the request, the HTTP status and the message of GitHub's JSON
    error body. Never the request's headers, which hold the token."""
    try:
        message = json.loads(error.read().decode("utf-8", "replace")).get("message")
    except (OSError, ValueError, AttributeError):
        message = None
    message = " ".join(str(message or error.reason).split())
    if len(message) > 200:
        message = message[:197] + "..."
    return f"GitHub API {method} {path} failed: HTTP {error.code}: {message}"


class GitHub:
    """The REST calls of the policy and the reports."""

    def __init__(self, repo, token):
        self.repo = repo
        self.token = token

    def call(self, method, path, body=None, level="error", quiet=()):
        """An HTTP error becomes an annotation at level, unless its status is in quiet."""
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"Bearer {self.token}",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in quiet:
                annotate(level, describe(method, path, error))
            raise

    def permission(self, user):
        try:
            answer = self.call("GET", f"collaborators/{urllib.parse.quote(user)}/permission",
                               quiet=(404,))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return "none"
            raise
        return answer.get("role_name") or answer.get("permission") or "none"

    def pull(self, number):
        return self.call("GET", f"pulls/{number}")

    def check_runs(self, sha):
        return self.call("GET", f"commits/{sha}/check-runs?per_page=100")["check_runs"]

    def statuses(self, sha):
        """The commit statuses of sha, newest first."""
        return self.call("GET", f"commits/{sha}/statuses?per_page=100")

    def request_runs(self, actor, since):
        query = urllib.parse.urlencode({"event": "issue_comment", "actor": actor, "per_page": 100,
                                        "created": ">=" + since.strftime("%Y-%m-%dT%H:%M:%SZ")})
        runs = []
        for page in range(1, 51):
            batch = self.call("GET", f"actions/workflows/test-requests.yml/runs?{query}&page={page}")
            runs += batch["workflow_runs"]
            if len(batch["workflow_runs"]) < 100:
                return runs
        raise RuntimeError("over 5000 request runs in 24 hours; not guessing the quota")

    def run_jobs(self, run_id):
        return self.call("GET", f"actions/runs/{run_id}/jobs?per_page=100")["jobs"]

    def comment(self, number, body, level="error"):
        self.call("POST", f"issues/{number}/comments", {"body": body}, level)

    def set_status(self, sha, context, state, description, url):
        self.call("POST", f"statuses/{sha}", {"state": state, "description": description[:140],
                                               "target_url": url, "context": context})


class Fixtures:
    """API answers from a JSON object instead of GitHub; writes are printed, not sent.
    An entry "fail": {"comment": {"status": 403, "body": {"message": ...}}} makes that
    write fail as GitHub.call fails, with that HTTP status and JSON error body."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def fail(self, name, method, path, level="error"):
        failure = self.answers.get("fail", {}).get(name)
        if failure:
            error = urllib.error.HTTPError(path, failure["status"], "fixture", None,
                                           io.BytesIO(json.dumps(failure.get("body", {})).encode()))
            annotate(level, describe(method, path, error))
            raise error

    def permission(self, user):
        self.calls.append(f"permission {user}")
        return self.answers.get("permissions", {}).get(user, "none")

    def pull(self, number):
        self.calls.append(f"pull {number}")
        return self.answers["pull"]

    def check_runs(self, sha):
        self.calls.append(f"check-runs {sha}")
        return self.answers.get("check_runs", {}).get(sha, [])

    def statuses(self, sha):
        self.calls.append(f"statuses {sha}")
        return self.answers.get("statuses", {}).get(sha, [])

    def request_runs(self, actor, since):
        self.calls.append(f"request-runs {actor}")
        return self.answers.get("runs", [])

    def run_jobs(self, run_id):
        self.calls.append(f"jobs {run_id}")
        return self.answers.get("jobs", {}).get(str(run_id), [])

    def comment(self, number, body, level="error"):
        self.fail("comment", "POST", f"issues/{number}/comments", level)
        print(f"[fixture] comment on #{number}:\n{body}")

    def set_status(self, sha, context, state, description, url):
        self.fail("set_status", "POST", f"statuses/{sha}")
        print(f"[fixture] status {context}={state} on {sha}: {description} ({url})")


def api_from_env():
    fixtures = os.environ.get("AF_CI_REQUEST_FIXTURES")
    if fixtures:
        if os.environ.get("ACT") != "true":
            sys.exit("AF_CI_REQUEST_FIXTURES is honoured only in act runs (ACT=true)")
        with open(fixtures) as file:
            return Fixtures(json.load(file))
    return GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])


def settings_from_env():
    return {"gpu_tests": os.environ.get("AF_CI_GPU_TESTS", ""),
            "gpu_runner": os.environ.get("AF_CI_GPU_RUNNER", ""),
            "accelerator_builds": os.environ.get("AF_CI_ACCELERATOR_BUILDS", ""),
            "bot": os.environ.get("AF_CI_CLAUDE_BOT", ""),
            "per_pr_day": int(os.environ.get("AF_CI_GPU_AI_PER_PR_DAY") or 2),
            "per_day": int(os.environ.get("AF_CI_GPU_AI_PER_DAY") or 6),
            "run_id": os.environ.get("GITHUB_RUN_ID", "")}


def write_outputs(values):
    delimiter = f"END_{secrets.token_hex(16)}"
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in values.items():
            output.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def final_state(command, result, summary):
    """The commit status state and text for the called workflow's result."""
    if result == "success" and command == "gpu" and not summary:
        return "error", "did not run: gpu.yml's test job was skipped"
    if result == "success":
        return "success", f"passed: {summary}" if summary else "passed"
    if result == "failure":
        return "failure", f"failed: {summary}" if summary else "failed"
    if result == "cancelled":
        return "error", (f"cancelled, for example replaced in the queue by a later request; "
                         f"ask again with /{command}-tests")
    return "error", f"did not complete (result: {result or 'unknown'})"


def comment_after_status(api, number, body):
    """Posts a comment whose failure doesn't fail the job: the commit status, already set,
    is the result, and the comment is a courtesy."""
    try:
        api.comment(number, body, level="warning")
    except urllib.error.HTTPError:
        print(f"Not commenting on #{number}; the commit status is set.")
    except OSError as error:
        annotate("warning", f"Not commenting on #{number}; the commit status is set: {error}")


def main(command):
    with open(os.environ["GITHUB_EVENT_PATH"]) as file:
        event = json.load(file)
    number = (event.get("issue") or {}).get("number")
    api = api_from_env()
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")

    if command == "decide":
        result = decide(event, settings_from_env(), api, datetime.datetime.now(datetime.timezone.utc))
        print(f"decision: {result['action']} ({result['reason']}) command={result['command'] or '-'} "
              f"sha={result['sha'] or '-'} cuda={result['cuda'] or '-'} "
              f"accelerators={str(result['accelerators']).lower()} "
              f"macos-windows={str(result['macos_windows']).lower()}")
        if result["reply"]:
            print(f"reply: {result['reply']}")
        write_outputs({"decision": result["action"], "reason": result["reason"],
                       "command": result["command"], "sha": result["sha"], "cuda": result["cuda"],
                       "accelerators": str(result["accelerators"]).lower(),
                       "macos-windows": str(result["macos_windows"]).lower(),
                       "reply": result["reply"]})
    elif command == "reply":
        api.comment(number, os.environ["REPLY"])
    elif command == "accepted":
        kind, sha = os.environ["COMMAND"], os.environ["SHA"]
        if kind == "gpu":
            text = f"GPU tests (CUDA {os.environ['CUDA']}): building, then waiting for a maintainer's approval"
        else:
            text = "CPU tests queued"
        api.set_status(sha, f"{kind}-tests", "pending", text, run_url)
        comment_after_status(api, number, f"{os.environ['REPLY']} [Run]({run_url})")
    elif command == "report":
        kind, sha = os.environ["COMMAND"], os.environ["SHA"]
        state, text = final_state(kind, os.environ.get("RESULT", ""), os.environ.get("SUMMARY", ""))
        what = f"GPU tests (CUDA {os.environ['CUDA']})" if kind == "gpu" else "CPU tests"
        api.set_status(sha, f"{kind}-tests", state, f"{what} {text}", run_url)
        comment_after_status(api, number, f"{what} of `{sha}` {text} [Run]({run_url})")
    else:
        sys.exit(f"unknown command: {command}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
