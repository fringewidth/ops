#!/usr/bin/env python3
"""ops: tasks and claims as YAML in git, numbers pushed to a Worker.

Every write pulls, edits, validates, commits and pushes under a file lock, so
several sessions on one machine and machines elsewhere can write at once.
Standard library only: runs with any python3 >= 3.10, no install.
See README.md for the schema.
"""

import argparse
import contextlib
import datetime as dt
import difflib
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



# ---------- a small YAML subset (block maps and lists, flow lists and maps, quoted and block scalars) ----------

class YAMLError(ValueError):
    pass


_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_./(~$][^\n]*$")
_NUMBER = re.compile(r"^[-+]?(\d[\d_]*)?(\.\d+)?([eE][-+]?\d+)?$")


def _scalar(text):
    text = text.strip()
    if not text:
        return None
    if text[0] == '"':
        end = _quoted_end(text, 0)
        return json.loads(text[: end + 1])
    if text[0] == "'":
        end = _quoted_end(text, 0)
        return text[1:end].replace("''", "'")
    if text[0] == "[":
        return [_scalar(part) for part in _split_flow(text[1:_matching(text, 0)])]
    if text[0] == "{":
        out = {}
        for part in _split_flow(text[1:_matching(text, 0)]):
            key, _, value = part.partition(":")
            out[_scalar(key)] = _scalar(value)
        return out
    cut = re.search(r"\s#", text)
    if cut:
        text = text[: cut.start()].rstrip()
    if text in ("null", "Null", "NULL", "~"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _NUMBER.match(text) and any(c.isdigit() for c in text):
        try:
            return int(text.replace("_", ""))
        except ValueError:
            return float(text.replace("_", ""))
    return text


def _quoted_end(text, start):
    quote, i = text[start], start + 1
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    raise YAMLError(f"unterminated string: {text}")


def _matching(text, start):
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c in "\"'":
            i = _quoted_end(text, i)
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise YAMLError(f"unclosed bracket: {text}")


def _split_flow(text):
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c in "\"'":
            i = _quoted_end(text, i)
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _key_split(content):
    """'key: rest' -> (key, rest), or None if the line is not a mapping entry."""
    if content[0] in "\"'":
        end = _quoted_end(content, 0)
        if content[end + 1 : end + 2] == ":":
            return _scalar(content[: end + 1]), content[end + 2 :].strip()
        return None
    match = re.match(r"^([^\s:#][^:#]*?)\s*:(?:\s+(.*)|$)", content)
    return (match.group(1), (match.group(2) or "").strip()) if match else None


def yaml_load(text):
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if raw.startswith("---") or raw.startswith("..."):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped, raw))
    value, _ = _block(lines, _next(lines, 0), None)
    return value


def _next(lines, i):
    while i < len(lines) and (not lines[i][1] or lines[i][1].startswith("#")):
        i += 1
    return i


def _block(lines, i, parent_indent):
    if i >= len(lines):
        return None, i
    indent = lines[i][0]
    if parent_indent is not None and indent < parent_indent:
        return None, i
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        return _sequence(lines, i, indent)
    return _mapping(lines, i, indent)


def _value_after(lines, i, indent, rest, allow_same_indent_seq):
    """Value for a key or item whose inline text is rest; i points at the next line."""
    if rest in ("|", "|-", "|+", ">", ">-", ">+"):
        body, j = [], i
        while j < len(lines) and (not lines[j][1] or lines[j][0] > indent):
            body.append(lines[j][2])
            j += 1
        while body and not body[-1].strip():
            body.pop()
        cut = min((len(b) - len(b.lstrip(" ")) for b in body if b.strip()), default=0)
        body = [b[cut:] for b in body]
        text = "\n".join(body) if rest[0] == "|" else " ".join(b.strip() for b in body)
        return (text if rest.endswith("-") else text + "\n"), j
    j = _next(lines, i)
    if rest:
        value = _scalar(rest)
        # plain scalar continued on more-indented lines
        while isinstance(value, str) and rest[0] not in "\"'[{" and j < len(lines) and lines[j][0] > indent and _key_split(lines[j][1]) is None and not lines[j][1].startswith("- "):
            value += " " + lines[j][1]
            j = _next(lines, j + 1)
        return value, j
    if j < len(lines) and lines[j][0] > indent:
        return _block(lines, j, indent + 1)
    if allow_same_indent_seq and j < len(lines) and lines[j][0] == indent and lines[j][1].startswith("-"):
        return _sequence(lines, j, indent)
    return None, j


def _mapping(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        split = _key_split(lines[i][1])
        if split is None:
            raise YAMLError(f"cannot parse line: {lines[i][2]}")
        key, rest = split
        out[key], i = _value_after(lines, i + 1, indent, rest, True)
        i = _next(lines, i)
    return out, i


def _sequence(lines, i, indent):
    out = []
    while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
        rest = lines[i][1][1:].strip()
        if rest and rest[0] not in "\"'[{" and _key_split(rest):
            # a mapping that starts on the dash line: re-read it as a block at the item's indent
            item_indent = indent + 2
            virtual = [(item_indent, rest, " " * item_indent + rest)]
            j = i + 1
            while j < len(lines) and (not lines[j][1] or lines[j][0] >= item_indent):
                virtual.append(lines[j])
                j += 1
            value, _ = _mapping(virtual, 0, item_indent)
            out.append(value)
            i = _next(lines, j)
            continue
        value, i = _value_after(lines, i + 1, indent, rest, False)
        out.append(value)
        i = _next(lines, i)
    return out, i


def _emit_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    plain = (
        _PLAIN_SAFE.match(text)
        and text == text.strip()
        and ": " not in text
        and " #" not in text
        and not text.endswith(":")
        and not isinstance(_scalar(text), (bool, int, float, type(None)))
        and not re.match(r"^(yes|no|on|off|y|n)$", text, re.I)  # booleans to YAML 1.1 readers
        and not re.match(r"^\d{4}-\d\d-\d\d", text)  # timestamps to other readers
    )
    return text if plain else json.dumps(text, ensure_ascii=False)


def yaml_dump(value, indent=0):
    pad = " " * indent
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            if isinstance(item, dict) and item:
                out.append(f"{pad}{_emit_scalar(key)}:\n{yaml_dump(item, indent + 2)}")
            elif isinstance(item, list) and item:
                out.append(f"{pad}{_emit_scalar(key)}:\n{yaml_dump(item, indent)}")
            else:
                empty = "{}" if isinstance(item, dict) else "[]" if isinstance(item, list) else _emit_scalar(item)
                out.append(f"{pad}{_emit_scalar(key)}: {empty}\n")
        return "".join(out)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                out.append(f"{pad}- {json.dumps(item, ensure_ascii=False)}\n")
            else:
                out.append(f"{pad}- {_emit_scalar(item)}\n")
        return "".join(out)
    return f"{pad}{_emit_scalar(value)}\n"


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
    return (yaml_load(path.read_text()) or {}) if path.exists() else {}


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
    git("add", "-A", "projects", HIDDEN.name)
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
        node = yaml_load(path.read_text()) or {}
        node["id"] = path.stem
        nodes[node["id"]] = node
    return nodes


def load_messages(project):
    messages = []
    for path in sorted((project_dir(project) / "messages").glob("*.yaml")):
        message = yaml_load(path.read_text()) or {}
        message["id"] = path.stem
        messages.append(message)
    return messages


def write_node(project, node):
    body = {key: value for key, value in node.items() if key != "id" and value not in (None, [], {}, "")}
    path = project_dir(project) / "nodes" / f"{node['id']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_dump(body))


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
    """key=value on the command line: lists, maps, numbers, booleans and null are parsed; anything else stays text."""
    if text == "":
        return None
    stripped = text.strip()
    if stripped[:1] in "[{" or stripped[:1] in "\"'":
        try:
            return _scalar(stripped)
        except (YAMLError, ValueError):
            return text
    value = _scalar(stripped)
    return value if isinstance(value, (bool, int, float, type(None))) else text


def set_path(node, dotted, value):
    keys = dotted.split(".")
    target = node
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    if value is None:
        target.pop(keys[-1], None)
    else:
        target[keys[-1]] = value


def version(node):
    """The stamp `ops show` prints and --expect compares: when the node last changed."""
    if not node:
        return "new"
    return str(node.get("updated") or node.get("created") or "never")


def node_path(project, node_id):
    return project_dir(project) / "nodes" / f"{node_id}.yaml"


def expect_unchanged(args, nodes):
    """Refuse with a diff when the node changed since the reader saw `--expect`; without it, print a reminder."""
    node = nodes.get(args.id)
    if args.expect is None:
        print(f"ops: reminder: read before you write: `ops show {args.id}`, then pass --expect '{version(node)}'", file=sys.stderr)
        return
    if version(node) == args.expect:
        return
    path = node_path(args.project, args.id)
    seen = None
    log = git("log", "-n", "60", "--format=%H", "--", str(path.relative_to(HOME)), check=False).stdout.split()
    for commit in log:
        text = git("show", f"{commit}:{path.relative_to(HOME)}", check=False).stdout
        if text and version(yaml_load(text) or {}) == args.expect:
            seen = text
            break
    lines = [f"ops: refused: {args.id} changed since {args.expect} (now {version(node)}); read it again with `ops show {args.id}`"]
    if seen is not None and path.exists():
        diff = list(difflib.unified_diff(seen.splitlines(), path.read_text().splitlines(), "as you read it", "now", lineterm="", n=0))
        lines += diff[2:42] + (["  ..."] if len(diff) > 42 else [])
    sys.exit("\n".join(lines))


def mutate(args, change, message):
    """Pull, apply change(nodes) -> list of node ids touched, validate, commit, push."""
    with locked():
        pull()
        nodes = load_nodes(args.project)
        if hasattr(args, "expect"):
            expect_unchanged(args, nodes)
        touched = change(nodes)
        for node_id in touched:
            nodes[node_id]["updated"] = stamp()
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
        (folder / f"{message_id}.yaml").write_text(yaml_dump({k: v for k, v in body.items() if v}))
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
    if args.id:
        with locked():
            pull()
    nodes = load_nodes(args.project)
    if args.id:
        if args.id not in nodes:
            sys.exit(f"ops: no node {args.id}")
        print(yaml_dump(nodes[args.id]).rstrip())
        children = [n["id"] for n in nodes.values() if n.get("parent") == args.id]
        if children:
            print("children:", ", ".join(children))
        print(f"updated: {version(nodes[args.id])}   (write with --expect '{version(nodes[args.id])}')")
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


# ---------- deleting series ----------
# The worker deletes a series and its points on DELETE /series?id= (token). A worker deployed before
# that endpoint answers the request like a GET; then the series are hidden instead: their ids go into
# hidden-series.json with the last report time, the site leaves them out, and a series that reports
# again after being hidden shows up again.
HIDDEN = Path(__file__).resolve().parent / "hidden-series.json"


def worker_request(path, method="GET"):
    values = env()
    url = values.get("OPS_WORKER") or repo_config().get("worker")
    headers = {"User-Agent": "ops/1"}
    if method != "GET":
        headers["Authorization"] = f"Bearer {values.get('OPS_TOKEN', '')}"
    request = urllib.request.Request(f"{url.rstrip('/')}{path}", method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null")


def cmd_rm_series(args):
    every = worker_request("/series")
    chosen = {}
    for target in args.series:
        if target.isdigit():
            chosen.update((s["id"], s) for s in every if s["id"] == int(target))
            continue
        name, _, rest = target.partition("{")
        labels = dict(re.findall(r'(\w+)="([^"]*)"', rest))
        chosen.update((s["id"], s) for s in every if s["name"] == name and all(s["labels"].get(k) == v for k, v in labels.items()))
    if args.stale_hours is not None:
        cutoff = time.time() * 1000 - args.stale_hours * 3600000
        chosen.update((s["id"], s) for s in every if s["last_t"] < cutoff)
    if not chosen:
        sys.exit("ops: no series matched")
    for s in sorted(chosen.values(), key=lambda s: (s["name"], json.dumps(s["labels"]))):
        print(f'{s["id"]:>5}  {s["name"]}{json.dumps(s["labels"]) if s["labels"] else ""}')
    if args.dry_run:
        print(f"{len(chosen)} series would be removed (dry run)")
        return
    answer = worker_request(f"/series?id={','.join(map(str, sorted(chosen)))}", method="DELETE")
    if isinstance(answer, dict) and "deleted" in answer:
        print(f"deleted {answer['deleted']} series and their points")
        return
    with locked():
        pull()
        hidden = json.loads(HIDDEN.read_text()) if HIDDEN.exists() else {}
        hidden.update({str(i): s["last_t"] for i, s in chosen.items()})
        HIDDEN.write_text(json.dumps(hidden, indent=0, sort_keys=True) + "\n")
        commit_and_push(f"hide {len(chosen)} series ({args.who or 'ops'})")
    print(f"hid {len(chosen)} series (the deployed worker has no delete yet; deploy worker/ to delete for real)")


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
    state = {"built": now(), "config": repo_config(), "hidden": json.loads(HIDDEN.read_text()) if HIDDEN.exists() else {}, "projects": {}}
    for folder in sorted(PROJECTS.iterdir()) if PROJECTS.exists() else []:
        if not folder.is_dir():
            continue
        meta = yaml_load((folder / "project.yaml").read_text()) if (folder / "project.yaml").exists() else {}
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


def stamp():
    """`updated`: to the second, so --expect tells apart two writes in one minute."""
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


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
    command.add_argument("--expect", help="the `updated` stamp `ops show <id>` printed; refuse if the node changed since")
    node_options(command)
    command.set_defaults(func=cmd_set)

    command = sub.add_parser("claim", help="mark a node running on this host (creates it if new)")
    command.add_argument("id")
    command.add_argument("--pid", type=int)
    command.add_argument("--cmd")
    command.add_argument("--ends", help="expected end, e.g. '2026-11-10 17:00'")
    command.add_argument("--expect", help="the `updated` stamp `ops show <id>` printed; refuse if the node changed since")
    node_options(command)
    command.set_defaults(func=cmd_claim)

    command = sub.add_parser("done", help="finish a node")
    command.add_argument("id")
    command.add_argument("--result", help="one line: what happened and where the output is")
    command.add_argument("--failed", action="store_true")
    command.add_argument("--dropped", action="store_true")
    command.add_argument("--expect", help="the `updated` stamp `ops show <id>` printed; refuse if the node changed since")
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

    command = sub.add_parser("rm-series", help="delete metric series by id or selector name{k=\"v\"} (hides them until the worker can delete)")
    command.add_argument("series", nargs="*")
    command.add_argument("--stale-hours", type=float, help="also every series nothing has reported for this many hours")
    command.add_argument("--dry-run", action="store_true", help="list what would go and stop")
    command.set_defaults(func=cmd_rm_series)

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
