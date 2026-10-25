#!/usr/bin/env python3
"""Handles "/gpu-tests" requests on pull requests for gpu-request.yml.

  gpu-request.py decide     decide on the issue_comment event in GITHUB_EVENT_PATH and
                            write decision, sha, cuda and reply to GITHUB_OUTPUT
  gpu-request.py reply      post REPLY on the pull request
  gpu-request.py accepted   set the pending gpu-tests status on SHA and post REPLY
  gpu-request.py report     set the final gpu-tests status from RESULT and SUMMARY
                            (gpu.yml's result and test summary) and comment

decide() depends only on the event, the settings and the API answers. With
AF_CI_GPU_REQUEST_FIXTURES naming a JSON file of API answers, those replace GitHub and
nothing is written; that is honoured only in act runs (ACT=true).
"""
import datetime
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

COMMAND = re.compile(r"/gpu-tests(?:\s+cuda=(\d+\.\d+))?")
USAGE = "Usage: `/gpu-tests` or `/gpu-tests cuda=<major.minor>`, for example `/gpu-tests cuda=13.1`."
DEFAULT_CUDA = "12.9"  # gpu.yml's default
REQUIRED_CHECKS = ("lint", "docs", "linux-gcc-openblas", "linux-clang-openblas")
WRITE_ROLES = {"admin", "maintain", "write"}
LOOP_BOT = "github-actions[bot]"


def decision(action, reason, reply="", sha="", cuda=""):
    return {"action": action, "reason": reason, "reply": reply, "sha": sha, "cuda": cuda}


def decide(event, settings, api, now):
    """Returns the decision on one issue_comment event; asks api only what it needs."""
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    login = (comment.get("user") or {}).get("login", "")
    lines = (comment.get("body") or "").splitlines()
    first = lines[0].strip() if lines else ""

    if not issue.get("pull_request"):
        return decision("ignore", "not-pull-request")
    if login == LOOP_BOT:
        return decision("ignore", "own-comment")
    if not re.match(r"/gpu-tests(\s|$)", first):
        return decision("ignore", "not-a-request")
    if settings["gate"] != "true":
        return decision("reply", "disabled", "GPU tests are disabled in this repository.")
    command = COMMAND.fullmatch(first)
    if not command:
        return decision("reply", "usage", USAGE)
    cuda = command.group(1) or DEFAULT_CUDA

    bot = settings["bot"]
    is_bot = bool(bot) and login == bot
    if not is_bot and api.permission(login) not in WRITE_ROLES:
        return decision("reply", "not-a-writer",
                        f"Sorry @{login}, only maintainers with write access can request GPU tests.")

    number = issue["number"]
    pull = api.pull(number)
    sha = pull["head"]["sha"]
    if is_bot:
        refusal = bot_refusal(settings, api, number, pull, sha, now)
        if refusal:
            reason, text = refusal
            return decision("reply", reason, f"Not starting GPU tests for `{sha}`: {text}")
    return decision("accept", "accepted",
                    f"GPU tests (CUDA {cuda}) requested by @{login} for `{sha}`, "
                    "the head of this pull request now. Later pushes are not part of this run.",
                    sha, cuda)


def bot_refusal(settings, api, number, pull, sha, now):
    """The limits on the Claude App's own requests; None when within them."""
    latest = {}
    for run in api.check_runs(sha):
        if run["name"] in REQUIRED_CHECKS and run["id"] > latest.get(run["name"], {}).get("id", -1):
            latest[run["name"]] = run
    red = [name for name in REQUIRED_CHECKS if (latest.get(name) or {}).get("conclusion") != "success"]
    if red:
        return "cpu-not-green", "the required CPU checks are not green on it: " + ", ".join(red) + "."

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


class GitHub:
    """The REST calls of the policy and the reports."""

    def __init__(self, repo, token):
        self.repo = repo
        self.token = token

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"Bearer {self.token}",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def permission(self, user):
        try:
            answer = self.call("GET", f"collaborators/{urllib.parse.quote(user)}/permission")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return "none"
            raise
        return answer.get("role_name") or answer.get("permission") or "none"

    def pull(self, number):
        return self.call("GET", f"pulls/{number}")

    def check_runs(self, sha):
        return self.call("GET", f"commits/{sha}/check-runs?per_page=100")["check_runs"]

    def request_runs(self, actor, since):
        query = urllib.parse.urlencode({"event": "issue_comment", "actor": actor, "per_page": 100,
                                        "created": ">=" + since.strftime("%Y-%m-%dT%H:%M:%SZ")})
        runs = []
        for page in range(1, 51):
            batch = self.call("GET", f"actions/workflows/gpu-request.yml/runs?{query}&page={page}")
            runs += batch["workflow_runs"]
            if len(batch["workflow_runs"]) < 100:
                return runs
        raise RuntimeError("over 5000 request runs in 24 hours; not guessing the quota")

    def run_jobs(self, run_id):
        return self.call("GET", f"actions/runs/{run_id}/jobs?per_page=100")["jobs"]

    def comment(self, number, body):
        self.call("POST", f"issues/{number}/comments", {"body": body})

    def status(self, sha, state, description, url):
        self.call("POST", f"statuses/{sha}", {"state": state, "description": description[:140],
                                               "target_url": url, "context": "gpu-tests"})


class Fixtures:
    """API answers from a JSON object instead of GitHub; writes are printed, not sent."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def permission(self, user):
        self.calls.append(f"permission {user}")
        return self.answers.get("permissions", {}).get(user, "none")

    def pull(self, number):
        self.calls.append(f"pull {number}")
        return self.answers["pull"]

    def check_runs(self, sha):
        self.calls.append(f"check-runs {sha}")
        return self.answers.get("check_runs", {}).get(sha, [])

    def request_runs(self, actor, since):
        self.calls.append(f"request-runs {actor}")
        return self.answers.get("runs", [])

    def run_jobs(self, run_id):
        self.calls.append(f"jobs {run_id}")
        return self.answers.get("jobs", {}).get(str(run_id), [])

    def comment(self, number, body):
        print(f"[fixture] comment on #{number}:\n{body}")

    def status(self, sha, state, description, url):
        print(f"[fixture] status gpu-tests={state} on {sha}: {description} ({url})")


def api_from_env():
    fixtures = os.environ.get("AF_CI_GPU_REQUEST_FIXTURES")
    if fixtures:
        if os.environ.get("ACT") != "true":
            sys.exit("AF_CI_GPU_REQUEST_FIXTURES is honoured only in act runs (ACT=true)")
        with open(fixtures) as file:
            return Fixtures(json.load(file))
    return GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])


def settings_from_env():
    return {"gate": os.environ.get("AF_CI_GPU_TESTS", ""),
            "bot": os.environ.get("AF_CI_CLAUDE_BOT", ""),
            "per_pr_day": int(os.environ.get("AF_CI_GPU_AI_PER_PR_DAY") or 2),
            "per_day": int(os.environ.get("AF_CI_GPU_AI_PER_DAY") or 6),
            "run_id": os.environ.get("GITHUB_RUN_ID", "")}


def write_outputs(values):
    delimiter = f"END_{secrets.token_hex(16)}"
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in values.items():
            output.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def main(command):
    with open(os.environ["GITHUB_EVENT_PATH"]) as file:
        event = json.load(file)
    number = (event.get("issue") or {}).get("number")
    api = api_from_env()
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")

    if command == "decide":
        result = decide(event, settings_from_env(), api, datetime.datetime.now(datetime.timezone.utc))
        print(f"decision: {result['action']} ({result['reason']}) sha={result['sha'] or '-'} "
              f"cuda={result['cuda'] or '-'}")
        if result["reply"]:
            print(f"reply: {result['reply']}")
        write_outputs({"decision": result["action"], "reason": result["reason"],
                       "sha": result["sha"], "cuda": result["cuda"], "reply": result["reply"]})
    elif command == "reply":
        api.comment(number, os.environ["REPLY"])
    elif command == "accepted":
        sha = os.environ["SHA"]
        api.status(sha, "pending", f"GPU tests (CUDA {os.environ['CUDA']}) queued", run_url)
        api.comment(number, f"{os.environ['REPLY']} [Run]({run_url})")
    elif command == "report":
        sha, cuda = os.environ["SHA"], os.environ["CUDA"]
        result, summary = os.environ.get("RESULT", ""), os.environ.get("SUMMARY", "")
        if result == "success" and summary:
            state, text = "success", f"passed: {summary}"
        elif result == "success":
            state, text = "error", "the GPU test job did not run (no GPU runner configured?)"
        elif result == "failure":
            state, text = "failure", f"failed: {summary or 'the build or the test job failed'}"
        elif result == "cancelled":
            state, text = "error", ("cancelled, for example replaced in the GPU queue by a later "
                                    "request; ask again with /gpu-tests")
        else:
            state, text = "error", f"did not complete (result: {result or 'unknown'})"
        api.status(sha, state, f"GPU tests {text}", run_url)
        api.comment(number, f"GPU tests (CUDA {cuda}) of `{sha}` {text} [Run]({run_url})")
    else:
        sys.exit(f"unknown command: {command}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
