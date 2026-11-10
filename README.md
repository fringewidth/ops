# ops

The shared state for the user's projects: the plan (a tree of goals and tasks with dependencies), who is running what on which machine, messages between agent sessions, and every number worth watching. The user reads it at https://hrishik.me/ops, often from a phone. You change the plan only with `ops`.

1. Plan and claims: YAML files in this repo, `projects/<project>/nodes/<id>.yaml`. `ops` pulls, edits, validates, commits and pushes under a lock. Never edit them by hand. Never write `state.json`; CI builds it.
1. Numbers: a Cloudflare Worker with D1 at https://ops.fringewidth.workers.dev. Anything holding the token can push to it from anywhere. Anyone can read it.
1. Site: `site/index.html`, deployed by `.github/workflows/pages.yml` on every push, so a change shows up there about a minute later.

## Setup (once per machine)

```sh
git clone https://github.com/fringewidth/ops ~/ops     # already done on the user's Mac
ln -sf ~/ops/ops.py ~/.local/bin/ops                   # plain python3 >= 3.10, standard library only
printf 'OPS_TOKEN=<ask the user>\nOPS_WORKER=https://ops.fringewidth.workers.dev\n' > ~/.config/ops/env
```

Pass `--as "<session> <topic>"` on every write, e.g. `--as "beebot-b3 compute"`. ListAgents gives your session name. You can set `OPS_SESSION` instead. The project defaults to the name of the git repo you run in. Otherwise pass `-p <project>`.

## Nodes

```yaml
title: Slot-word SFT                                    # required
summary: Fine-tune the 0.6B policy with slot words.     # required, exactly one sentence
why: The policy confuses cheese names. Slot words may fix that.   # required, at most two sentences, simple English
kind: task          # free text: goal, task, run, failure, mitigation, ...
status: todo        # todo | queued | running | done | failed | dropped
parent: <id>        # position in the tree
depends: [<id>]     # these must be done first; drives "ready" and the critical path
mitigates: <id>     # this node works around that failed node
needs: mac-gpu      # a resource from project.yaml; work on the same resource cannot run in parallel
est_h: 3.5          # estimated hours; drives the critical path
metric: {series: 'demo_val_loss{run="lm-slotwords"}', direction: down, target: 1.2}   # the one number it moves
claim: {session, host, pid, cmd, started, ends}         # written by `ops claim`
kaggle: owner/slug  # the kaggle collector reports this kernel's status
links: [logs/x.out, runs/x/]
result: one line, written by `ops done`
order: 10           # sort order among siblings
```

`ops` rejects a change that is missing a required field, has a summary longer than one sentence or a `why` longer than two, points at an id that does not exist, or creates a cycle in `parent` or `depends`. Every task reduces to one number: give it a `metric` whenever one exists.

`projects/<project>/project.yaml` holds the project's resources (`capacity`, optional `busy_series`, `hours_series`), plus `pins` (series shown on the Now tab).

## Commands

```sh
ops board                     # read before using a shared resource: running (with PID liveness), queue, ready, messages, log
ops show [id] [--all]         # the open tree, or one node in full
ops add <id> --title .. --summary .. --why .. [--parent --depends a,b --needs --est-h --metric .. --direction up|down --target .. --status queued --link ..]
ops set <id> key=value ...    # any field; dotted keys nest (metric.target=1.1); `key=` deletes; flags from `add` work too
ops claim <id> --pid <pid> --cmd "<command>" --ends "2026-11-10 17:00" [--needs mac-gpu]   # marks running; creates the node if new (then give title/summary/why)
ops done <id> --result "<what happened, where the output is>" [--failed | --dropped]
ops msg "<text>" [--to <session>]    # ops msg --rm <message-id> once it has been acted on
ops push 'name{k="v"} 1.23' ...      # push numbers (or pipe lines on stdin)
ops tail <log> [--label run=x]       # follow a log and push every number in its JSON lines
ops check                            # validate every project
```

## Rules for shared resources

1. Run `ops board` before you start anything on a resource with capacity, and claim it before you launch. A queued node (`--status queued`) is a reservation, served first come, first served. Only the user reorders the queue.
1. Trust processes over claims. `ops board` marks a dead PID on this host as DEAD. Finish a dead claim with `ops done <id> --failed --result "died, found by <you>"` and message its owner.
1. When you finish, run `ops done` with the result's location. Then message the session that owns the first queued node for that resource.
1. When two sessions need a decision (priority, killing a run), ask the user through ntfy.sh/hrishik.

## Failures and replanning

A run that fails stays as a node: run `ops done <id> --failed --result ..`. Add the workaround as a new node with `--mitigates <id>`, and move dependents onto it with `ops set <dependent> depends=[<new>]`. To reorganize, change `parent`. Never delete a node: drop it with `ops done <id> --dropped`. Git history keeps the record of every change.

## Numbers

Line format: `name{label="value",...} value [unix_ms]`. Each distinct name plus label set is one series, and nothing else is assumed. Label a training run's numbers `run=<name>` and point the node's `metric.series` at them.

1. Push from a script with `ops push`, `ops tail`, or plain HTTP: `curl -X POST $OPS_WORKER/push -H "Authorization: Bearer $OPS_TOKEN" --data-binary @lines.txt`. A Kaggle kernel or a VPS pushes the same way. Send `User-Agent` other than Python's default, which Cloudflare blocks.
1. Pull: the Worker scrapes any public URL serving Prometheus text format every minute. Register one with `curl -X POST $OPS_WORKER/targets -H "Authorization: Bearer $OPS_TOKEN" -d '{"url":"https://host/metrics","labels":{"box":"vps1"}}'`.
1. Collectors: `ops agent` runs every executable in `collectors/` (all machines) and `~/.config/ops/collectors/` (this machine only) once a minute. It pushes what they print and adds `host=<hostname>`. To add a provider, write an executable that prints lines. Test it with `ops agent --once`. On the user's Mac, run the agent as a plain process (`nohup ops agent > ~/Library/Logs/ops-agent.log 2>&1 &`), never as a LaunchAgent: the Mac's endpoint security removed the launchd service on 2026-11-10, along with uv, brew and cloudflared. Collectors must never read credentials (keychain, tokens, cookies).
1. Read: `GET $OPS_WORKER/series[?q=substr]` returns every series with its last value. `GET $OPS_WORKER/points?id=1,2&since=<unix_ms>&max=<n>` returns the points, averaged into at most n buckets.

Existing collectors:

1. `mac`: `gpu_util_percent`, `gpu_mem_bytes`, `cpu_load1`, `mem_used_bytes`.
1. `claims`: `ops_claim_alive{project,node}`.
1. `kaggle`: `kaggle_kernel_status{slug}` for running nodes. Kaggle has no quota API, so update `kaggle_gpu_hours_left{account}` by hand with `ops push` after reading it on the site.
1. `disk`: `disk_free_bytes`, `disk_total_bytes` of the home volume.
1. `tools`: `tool_present{tool}` for the command-line tools the projects rely on (`OPS_TOOLS`, else uv, git, brew, cloudflared, node).
1. `http`: `http_up{project,node,url}` and `http_seconds` for every http link of a running node.
1. `vultr`: `vultr_credit_usd`, `vultr_pending_usd`, `vultr_remaining_usd` and `vultr_machines` every 5 minutes, when `VULTR_API_KEY` is set in `~/.config/ops/env`. The collector only sends GET requests, over IPv4 (the key's access control lists IPv4 addresses).
1. `vultr_gpu`: `vultr_gpu_regions{plan}`, the regions where each GPU plan can be ordered, every 5 minutes from Vultr's public catalogue (no key). It sends one ntfy notice to `OPS_NTFY_TOPIC` when a plan comes into stock, and never orders.
1. `claude_usage`: `claude_limit_percent{kind}` and `claude_limit_resets_in_hours{kind}` (kind: session, weekly_all, weekly_fable, ...) every 5 minutes, parsed from `claude -p "/usage"`. It reads the CLI's own report and never touches credentials; it replaces the keychain-reading collector removed on 2026-11-10.
1. `site_age`: `ops_site_age_minutes`, minutes since the published `state.json` was built. GitHub Pages deploys can fail while the build succeeds, which freezes the site.
1. `runs`: every number in the JSON lines of a running node's log links (`.out`, `.log`, `.jsonl`), labelled `run=<file stem>`, nested objects flattened with underscores. It keeps its read position in `~/.cache/ops-runs/`, so no `ops tail` process is needed on the host that holds the claim. Relative links resolve against `~/<project>`, or a line `<project> <path>` in `~/.config/ops/repos`.

## New project

Create `projects/<name>/project.yaml` (title, description, resources, pins), commit and push it. Then `ops add` its root goal from inside that project's repo, or with `-p <name>`.

## Site

The site has four tabs:

1. Now: insights, pinned numbers, running, queue, ready work grouped by resource, messages.
1. Plan: the tree, with ready nodes and the critical path marked. Tap a node to see it and edit it.
1. Numbers: every series. Tap to plot. Use `x:` to plot against another series, e.g. step instead of time.
1. History: recent changes.

Insights are computed on the page. They flag:

1. dead or overdue claims
1. free resource slots while work is ready
1. a busy GPU with no claim
1. a dependency that failed with no mitigation
1. a running node's metric moving the wrong way or flat
1. a silent pinned series

Edits on the site commit through the GitHub API with a fine-grained token that the user keeps in the browser.
