#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""ops: tasks and claims as YAML in git, numbers pushed to a Worker.

Every write pulls, edits, validates, commits and pushes under a file lock, so
several sessions on one machine and machines elsewhere can write at once.
See README.md for the schema.
"""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

HOME = Path(os.environ.get("OPS_HOME", Path(__file__).resolve().parent))
PROJECTS = HOME / "projects"
CONFIG_ENV = Path.home() / ".config" / "ops" / "env"
STATUSES = ["todo", "queued", "running", "done", "failed", "dropped"]
OPEN = {"todo", "queued", "running"}
REQUIRED = ["title", "summary", "why"]
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SENTENCE_END = re.compile(r"[.!?](?:\s+|$)")


# ---------- config and git ----------

def env():
    values = dict(os.environ)
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip())
    return values


def repo_config():
    path = HOME / "config.yaml"
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(HOME), *args], capture_output=True, text=True, check=check)


def has_remote():
    return bool(git("remote", check=False).stdout.strip())


@contextlib.contextmanager
def locked():
    lock = open(HOME / ".git" / "ops.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def pull():
    if has_remote():
        git("pull", "--rebase", "--autostash", "-q", check=False)


def commit_and_push(message):
    git("add", "-A", "projects")
    staged = git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return
    git("commit", "-q", "-m", message)
    if not has_remote():
        return
    for _ in range(5):
        pushed = git("push", "-q", check=False)
        if pushed.returncode == 0:
            return
        git("pull", "--rebase", "-q", check=False)
    sys.exit("ops: push failed after 5 tries; the commit is local, run `git -C ~/ops push`")


# ---------- model ----------

def project_dir(project):
    return PROJECTS / project


def load_nodes(project):
    nodes = {}
    for path in sorted((project_dir(project) / "nodes").glob("*.yaml")):
        node = yaml.safe_load(path.read_text()) or {}
        node["id"] = path.stem
        nodes[node["id"]] = node
    return nodes


def load_messages(project):
    messages = []
    for path in sorted((project_dir(project) / "messages").glob("*.yaml")):
        message = yaml.safe_load(path.read_text()) or {}
        message["id"] = path.stem
        messages.append(message)
    return messages


def write_node(project, node):
    body = {key: value for key, value in node.items() if key != "id" and value not in (None, [], {}, "")}
    path = project_dir(project) / "nodes" / f"{node['id']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=1000))


def sentences(text):
    return len(SENTENCE_END.findall(str(text).strip())) or 1


def problems(nodes):
    found = []
    for node_id, node in nodes.items():
        if not ID_RE.match(node_id):
            found.append(f"{node_id}: id must be lowercase letters, digits and dashes")
        for key in REQUIRED:
            if not str(node.get(key) or "").strip():
                found.append(f"{node_id}: missing {key}")
        if node.get("summary") and sentences(node["summary"]) > 1:
            found.append(f"{node_id}: summary must be one sentence")
        if node.get("why") and sentences(node["why"]) > 2:
            found.append(f"{node_id}: why must be at most two sentences")
        if node.get("status", "todo") not in STATUSES:
            found.append(f"{node_id}: status must be one of {', '.join(STATUSES)}")
        for key in ["parent", "mitigates"]:
            if node.get(key) and node[key] not in nodes:
                found.append(f"{node_id}: {key} {node[key]} does not exist")
        for dependency in node.get("depends") or []:
            if dependency not in nodes:
                found.append(f"{node_id}: depends on {dependency}, which does not exist")
    found += cycles(nodes, lambda node: [node["parent"]] if node.get("parent") in nodes else [], "parent")
    found += cycles(nodes, lambda node: [d for d in node.get("depends") or [] if d in nodes], "depends")
    return found


def cycles(nodes, edges, label):
    state = {}
    found = []

    def visit(node_id, trail):
        if state.get(node_id) == "done":
            return
        if state.get(node_id) == "open":
            found.append(f"{label} cycle: {' -> '.join(trail + [node_id])}")
            return
        state[node_id] = "open"
        for nxt in edges(nodes[node_id]):
            visit(nxt, trail + [node_id])
        state[node_id] = "done"

    for node_id in nodes:
        visit(node_id, [])
    return found


def ready(nodes):
    """Open, unclaimed nodes whose dependencies are all done."""
    return [
        node for node in nodes.values()
        if node.get("status", "todo") in {"todo", "queued"}
        and all(nodes.get(d, {}).get("status") == "done" for d in node.get("depends") or [])
        and not any(child.get("parent") == node["id"] and child.get("status", "todo") in OPEN for child in nodes.values())
    ]


def alive(claim):
    if not claim or claim.get("host") != socket.gethostname() or not claim.get("pid"):
        return None
    try:
        os.kill(int(claim["pid"]), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------- commands ----------

def parse_value(text):
    if text == "":
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def set_path(node, dotted, value):
    keys = dotted.split(".")
    target = node
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    if value is None:
        target.pop(keys[-1], None)
    else:
        target[keys[-1]] = value


def mutate(args, change, message):
    """Pull, apply change(nodes) -> list of node ids touched, validate, commit, push."""
    with locked():
        pull()
        nodes = load_nodes(args.project)
        touched = change(nodes)
        errors = [p for p in problems(nodes) if p.split(":")[0] in touched or "cycle" in p]
        if errors:
            git("checkout", "--", "projects", check=False)
            sys.exit("ops: refused:\n  " + "\n  ".join(errors))
        for node_id in touched:
            write_node(args.project, nodes[node_id])
        commit_and_push(f"{args.project}: {message} ({args.who})")
    for node_id in touched:
        print(f"{node_id}: {nodes[node_id].get('status', 'todo')}")


def node_fields(args):
    fields = {}
    for key in ["title", "summary", "why", "kind", "status", "parent", "needs", "est_h", "notes", "mitigates", "kaggle"]:
        value = getattr(args, key, None)
        if value is not None:
            fields[key] = value
    if getattr(args, "depends", None) is not None:
        fields["depends"] = [d for d in args.depends.split(",") if d]
    if getattr(args, "link", None):
        fields["links"] = args.link
    if getattr(args, "metric", None):
        fields["metric"] = {"series": args.metric, "direction": args.direction or "down"}
        if args.target is not None:
            fields["metric"]["target"] = args.target
    return fields


def cmd_add(args):
    def change(nodes):
        if args.id in nodes:
            sys.exit(f"ops: {args.id} exists; use `ops set`")
        nodes[args.id] = {"id": args.id, "title": None, "summary": None, "why": None, "status": "todo", **node_fields(args), "created": now(), "by": args.who}
        return [args.id]
    mutate(args, change, f"add {args.id}")


def cmd_set(args):
    def change(nodes):
        if args.id not in nodes:
            sys.exit(f"ops: no node {args.id}")
        node = nodes[args.id]
        for key, value in node_fields(args).items():
            node[key] = value
        for pair in args.pairs:
            if "=" not in pair:
                sys.exit(f"ops: expected key=value, got {pair}")
            key, value = pair.split("=", 1)
            set_path(node, key, parse_value(value))
        node["updated"] = now()
        return [args.id]
    mutate(args, change, f"set {args.id} {' '.join(args.pairs)}".strip())


def cmd_claim(args):
    def change(nodes):
        node = nodes.get(args.id) or {"id": args.id, "created": now(), "by": args.who, "kind": "run"}
        node.update(node_fields(args))
        node["status"] = "running"
        node["claim"] = {
            "session": args.who,
            "host": socket.gethostname(),
            "pid": args.pid,
            "cmd": args.cmd,
            "started": now(),
            "ends": args.ends,
        }
        node["claim"] = {key: value for key, value in node["claim"].items() if value is not None}
        nodes[args.id] = node
        return [args.id]
    mutate(args, change, f"claim {args.id}")


def cmd_done(args):
    def change(nodes):
        if args.id not in nodes:
            sys.exit(f"ops: no node {args.id}")
        node = nodes[args.id]
        node["status"] = "failed" if args.failed else "dropped" if args.dropped else "done"
        if args.result:
            node["result"] = args.result
        if node.get("claim"):
            node["claim"]["finished"] = now()
        node["updated"] = now()
        return [args.id]
    mutate(args, change, f"{'fail' if args.failed else 'drop' if args.dropped else 'finish'} {args.id}")


def cmd_msg(args):
    with locked():
        pull()
        folder = project_dir(args.project) / "messages"
        folder.mkdir(parents=True, exist_ok=True)
        if args.rm:
            for message_id in args.rm:
                (folder / f"{message_id}.yaml").unlink(missing_ok=True)
            commit_and_push(f"{args.project}: remove messages {' '.join(args.rm)} ({args.who})")
            return
        message_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        body = {"at": now(), "from": args.who, "to": args.to, "text": " ".join(args.text)}
        (folder / f"{message_id}.yaml").write_text(yaml.safe_dump({k: v for k, v in body.items() if v}, sort_keys=False, allow_unicode=True, width=1000))
        commit_and_push(f"{args.project}: message from {args.who}")
        print(message_id)


def cmd_board(args):
    with locked():
        pull()
    nodes = load_nodes(args.project)
    running = [n for n in nodes.values() if n.get("status") == "running"]
    queued = sorted((n for n in nodes.values() if n.get("status") == "queued"), key=lambda n: n.get("created", ""))
    print(f"# {args.project} board ({now()})\n\n## Running")
    for node in running:
        claim = node.get("claim") or {}
        state = {True: "alive", False: "DEAD", None: "elsewhere" if claim.get("host") else "no pid"}[alive(claim)]
        print(f"- {node['id']} [{node.get('needs', '-')}] {claim.get('session', '?')} pid {claim.get('pid', '-')} on {claim.get('host', '-')} ({state}); started {claim.get('started', '?')}, ends {claim.get('ends', '?')}: {node.get('title')}")
    print("\n## Queue (first come, first served)")
    for node in queued:
        print(f"- {node['id']} [{node.get('needs', '-')}] {node.get('est_h', '?')} h, by {node.get('by', '?')}: {node.get('title')}")
    print("\n## Ready (dependencies done, unclaimed)")
    for node in ready(nodes):
        print(f"- {node['id']} [{node.get('needs', '-')}] {node.get('title')}")
    print("\n## Messages")
    for message in load_messages(args.project):
        to = f" to {message['to']}" if message.get("to") else ""
        print(f"- {message['id']} {message.get('from')}{to}: {message.get('text')}")
    print("\n## Log")
    print(git("log", "-15", "--format=- %ad %s", "--date=format:%m-%d %H:%M", "--", f"projects/{args.project}", check=False).stdout.rstrip())


def cmd_show(args):
    nodes = load_nodes(args.project)
    if args.id:
        if args.id not in nodes:
            sys.exit(f"ops: no node {args.id}")
        print(yaml.safe_dump(nodes[args.id], sort_keys=False, allow_unicode=True, width=1000))
        children = [n["id"] for n in nodes.values() if n.get("parent") == args.id]
        if children:
            print("children:", ", ".join(children))
        return
    show_all = args.all

    def walk(parent, depth):
        for node in sorted((n for n in nodes.values() if n.get("parent") == parent), key=lambda n: (n.get("order", 0), n["id"])):
            if not show_all and node.get("status") in {"done", "dropped"}:
                continue
            depends = f" <- {','.join(node['depends'])}" if node.get("depends") else ""
            print(f"{'  ' * depth}- [{node.get('status', 'todo')}] {node['id']}: {node.get('title')}{depends}")
            walk(node["id"], depth + 1)

    walk(None, 0)


def cmd_check(args):
    bad = False
    for folder in sorted(PROJECTS.iterdir()) if PROJECTS.exists() else []:
        for problem in problems(load_nodes(folder.name)):
            bad = True
            print(f"{folder.name}: {problem}")
    sys.exit(1 if bad else 0)


def cmd_push(args):
    lines = args.lines or [line for line in sys.stdin.read().splitlines()]
    push_lines(lines, args.label)


def push_lines(lines, labels=None):
    values = env()
    url = values.get("OPS_WORKER") or repo_config().get("worker")
    token = values.get("OPS_TOKEN")
    if not url or not token:
        sys.exit("ops: set OPS_WORKER and OPS_TOKEN in ~/.config/ops/env")
    body = "\n".join(line for line in lines if line.strip())
    extra = ",".join(f'{k}="{v}"' for k, v in (pair.split("=", 1) for pair in labels or []))
    request = urllib.request.Request(f"{url.rstrip('/')}/push" + (f"?labels={urllib.parse.quote(extra)}" if extra else ""), data=body.encode(), method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain", "User-Agent": "ops/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def json_to_lines(record, labels):
    label_text = ",".join(f'{k}="{v}"' for k, v in labels.items())
    out = []
    for key, value in record.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        name = re.sub(r"[^a-zA-Z0-9_]", "_", key)
        out.append(f"{name}{{{label_text}}} {value}")
    return out


def cmd_tail(args):
    """Follow a log file and push every number in its JSON lines (one series per key)."""
    labels = dict(pair.split("=", 1) for pair in args.label or [])
    labels.setdefault("run", Path(args.path).stem)
    handle = open(args.path)
    if not args.from_start:
        handle.seek(0, 2)
    batch, last = [], time.time()
    while True:
        line = handle.readline()
        if line:
            start = line.find("{")
            if start >= 0:
                with contextlib.suppress(json.JSONDecodeError):
                    batch += json_to_lines(json.loads(line[start:]), labels)
        else:
            time.sleep(2)
        if batch and (time.time() - last > 15 or len(batch) > 500):
            with contextlib.suppress(Exception):
                push_lines(batch)
                batch = []
            last = time.time()
        if not line and args.once:
            if batch:
                push_lines(batch)
            return


def cmd_agent(args):
    """Run every executable collector every interval and push what it prints."""
    folders = [HOME / "collectors", Path.home() / ".config" / "ops" / "collectors"]
    host = socket.gethostname().split(".")[0]
    last_pull = 0
    while True:
        if time.time() - last_pull > 300:
            with locked():
                pull()
            last_pull = time.time()
        lines = []
        for folder in folders:
            for path in sorted(folder.glob("*")) if folder.exists() else []:
                if not os.access(path, os.X_OK) or path.is_dir():
                    continue
                try:
                    result = subprocess.run([str(path)], capture_output=True, text=True, timeout=60, env={**env(), "OPS_HOME": str(HOME)})
                    lines += [line for line in result.stdout.splitlines() if line.strip() and not line.startswith("#")]
                except Exception as error:  # a broken collector must not stop the others
                    print(f"{path.name}: {error}", file=sys.stderr)
        if lines:
            try:
                push_lines(lines, [f"host={host}"])
            except Exception as error:
                print(f"push: {error}", file=sys.stderr)
        if args.once:
            print("\n".join(lines))
            return
        time.sleep(args.interval)


def cmd_build(args):
    """Write state.json for the site: every project's nodes, messages and recent log."""
    state = {"built": now(), "config": repo_config(), "projects": {}}
    for folder in sorted(PROJECTS.iterdir()) if PROJECTS.exists() else []:
        if not folder.is_dir():
            continue
        meta = yaml.safe_load((folder / "project.yaml").read_text()) if (folder / "project.yaml").exists() else {}
        log = git("log", "-40", "--format=%aI%x09%s", "--", f"projects/{folder.name}", check=False).stdout.splitlines()
        state["projects"][folder.name] = {
            "meta": meta or {},
            "nodes": list(load_nodes(folder.name).values()),
            "messages": load_messages(folder.name),
            "log": [dict(zip(["at", "text"], line.split("\t", 1))) for line in log],
        }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "state.json").write_text(json.dumps(state, default=str))


def now():
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")


def default_project():
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(top.stdout.strip()).name if top.returncode == 0 and Path(top.stdout.strip()) != HOME else None


def main():

    parser = argparse.ArgumentParser(prog="ops", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-p", "--project", default=os.environ.get("OPS_PROJECT") or default_project())
    parser.add_argument("--as", dest="who", default=os.environ.get("OPS_SESSION") or os.environ.get("USER", "someone"), help="session name, e.g. 'beebot-b3 compute'")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--project", default=argparse.SUPPRESS)
    common.add_argument("--as", dest="who", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    plain_add_parser = sub.add_parser
    sub.add_parser = lambda *a, **k: plain_add_parser(*a, parents=[common], **k)

    def node_options(command, required=False):
        command.add_argument("--title", required=required)
        command.add_argument("--summary", required=required, help="one sentence")
        command.add_argument("--why", required=required, help="at most two sentences, simple English")
        command.add_argument("--kind", help="goal, task, run, failure, mitigation, ...")
        command.add_argument("--status", choices=STATUSES)
        command.add_argument("--parent")
        command.add_argument("--depends", help="comma-separated ids")
        command.add_argument("--mitigates", help="id of the failure this works around")
        command.add_argument("--needs", help="resource, e.g. mac-gpu, kaggle")
        command.add_argument("--est-h", dest="est_h", type=float)
        command.add_argument("--metric", help="series selector, e.g. 'demo_val_loss{run=\"lm-x\"}'")
        command.add_argument("--direction", choices=["up", "down"])
        command.add_argument("--target", type=float)
        command.add_argument("--link", action="append")
        command.add_argument("--kaggle", help="kernel slug owner/name")
        command.add_argument("--notes")

    command = sub.add_parser("add", help="add a node")
    command.add_argument("id")
    node_options(command, required=True)
    command.set_defaults(func=cmd_add)

    command = sub.add_parser("set", help="change fields: flags or key=value (dotted keys nest, empty value deletes)")
    command.add_argument("id")
    command.add_argument("pairs", nargs="*")
    node_options(command)
    command.set_defaults(func=cmd_set)

    command = sub.add_parser("claim", help="mark a node running on this host (creates it if new)")
    command.add_argument("id")
    command.add_argument("--pid", type=int)
    command.add_argument("--cmd")
    command.add_argument("--ends", help="expected end, e.g. '2026-11-10 17:00'")
    node_options(command)
    command.set_defaults(func=cmd_claim)

    command = sub.add_parser("done", help="finish a node")
    command.add_argument("id")
    command.add_argument("--result", help="one line: what happened and where the output is")
    command.add_argument("--failed", action="store_true")
    command.add_argument("--dropped", action="store_true")
    command.set_defaults(func=cmd_done)

    command = sub.add_parser("msg", help="leave a message on the board")
    command.add_argument("text", nargs="*")
    command.add_argument("--to")
    command.add_argument("--rm", nargs="+", help="message ids to delete")
    command.set_defaults(func=cmd_msg)

    sub.add_parser("board", help="running, queue, ready, messages, log").set_defaults(func=cmd_board)

    command = sub.add_parser("show", help="the open tree, or one node")
    command.add_argument("id", nargs="?")
    command.add_argument("--all", action="store_true", help="include done and dropped")
    command.set_defaults(func=cmd_show)

    sub.add_parser("check", help="validate every project").set_defaults(func=cmd_check)

    command = sub.add_parser("push", help="push metric lines 'name{k=\"v\"} value [unix_ms]' (args or stdin)")
    command.add_argument("lines", nargs="*")
    command.add_argument("--label", action="append", help="k=v added to every line")
    command.set_defaults(func=cmd_push)

    command = sub.add_parser("tail", help="follow a log and push the numbers in its JSON lines")
    command.add_argument("path")
    command.add_argument("--label", action="append", help="k=v; run defaults to the file stem")
    command.add_argument("--from-start", action="store_true")
    command.add_argument("--once", action="store_true", help="push what is there and exit")
    command.set_defaults(func=cmd_tail)

    command = sub.add_parser("agent", help="run collectors forever and push their output")
    command.add_argument("--interval", type=int, default=60)
    command.add_argument("--once", action="store_true")
    command.set_defaults(func=cmd_agent)

    command = sub.add_parser("build", help="write state.json for the site")
    command.add_argument("out")
    command.set_defaults(func=cmd_build)

    args = parser.parse_args()
    needs_project = args.command in {"add", "set", "claim", "done", "msg", "board", "show"}
    if needs_project and not args.project:
        sys.exit("ops: no project; pass -p NAME or run inside the project's git repo")
    if needs_project and args.command not in {"board", "show"} and not project_dir(args.project).exists():
        sys.exit(f"ops: no project {args.project}; create projects/{args.project}/project.yaml first")
    args.func(args)


if __name__ == "__main__":
    main()
