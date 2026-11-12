// ops metrics store: a Cloudflare Worker over D1.
//
// POST /push            text lines `name{k="v",...} value [unix_ms]` (Prometheus text format subset).
//                       Query ?labels=k="v",... adds labels to every line. Needs the bearer token.
// GET  /series          every series with its last value (?q= filters by substring of name or labels).
// DELETE /series?id=1,2 removes those series and all their points (token).
// GET  /points?id=1,2   points per series (?since=unix_ms, ?max=N buckets, averaged).
// GET  /alerts          open alerts and the conditions as evaluated now (read-only; see alerts below).
// GET  /targets         scrape targets; POST {url, labels} adds one; DELETE ?url= removes one (token).
// Cron (every minute)   GETs each target URL and stores what it serves; drops points older than RETAIN_DAYS;
//                       posts alerts to ntfy (see alerts below).

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};

function reply(body, status = 200) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": typeof body === "string" ? "text/plain" : "application/json", ...CORS },
  });
}

function authorized(request, env) {
  return request.headers.get("Authorization") === `Bearer ${env.TOKEN}`;
}

const LINE = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+(\S+)(?:\s+(\d+))?\s*$/;
const LABEL = /([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"/g;

function parseLabels(text) {
  const labels = {};
  for (const match of (text || "").matchAll(LABEL)) labels[match[1]] = match[2];
  return labels;
}

function canonical(labels) {
  return JSON.stringify(Object.fromEntries(Object.entries(labels).sort(([a], [b]) => a.localeCompare(b))));
}

function parse(text, extra, now) {
  const samples = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const match = line.match(LINE);
    if (!match) continue;
    const value = Number(match[3]);
    if (!Number.isFinite(value)) continue;
    samples.push({
      name: match[1],
      labels: canonical({ ...parseLabels(match[2]), ...extra }),
      value,
      t: match[4] ? Number(match[4]) : now,
    });
  }
  return samples;
}

async function store(env, samples) {
  if (!samples.length) return 0;
  const ids = new Map();
  const keys = [...new Set(samples.map((s) => `${s.name}\u0000${s.labels}`))];
  for (let i = 0; i < keys.length; i += 50) {
    const chunk = keys.slice(i, i + 50);
    const rows = await env.DB.batch(
      chunk.map((key) => {
        const [name, labels] = key.split("\u0000");
        return env.DB.prepare(
          "INSERT INTO series (name, labels) VALUES (?, ?) ON CONFLICT(name, labels) DO UPDATE SET name = excluded.name RETURNING id"
        ).bind(name, labels);
      })
    );
    chunk.forEach((key, j) => ids.set(key, rows[j].results[0].id));
  }
  const last = new Map();
  const inserts = samples.map((s) => {
    const id = ids.get(`${s.name}\u0000${s.labels}`);
    if (!last.has(id) || last.get(id).t <= s.t) last.set(id, s);
    return env.DB.prepare("INSERT INTO points (series_id, t, v) VALUES (?, ?, ?)").bind(id, s.t, s.value);
  });
  for (const [id, s] of last) {
    inserts.push(env.DB.prepare("UPDATE series SET last_t = ?, last_v = ? WHERE id = ? AND (last_t IS NULL OR last_t <= ?)").bind(s.t, s.value, id, s.t));
  }
  for (let i = 0; i < inserts.length; i += 100) await env.DB.batch(inserts.slice(i, i + 100));
  return samples.length;
}

async function scrape(env) {
  const { results } = await env.DB.prepare("SELECT url, labels FROM targets").all();
  const now = Date.now();
  await Promise.all(
    results.map(async (target) => {
      try {
        const response = await fetch(target.url, { signal: AbortSignal.timeout(10000) });
        const extra = { instance: new URL(target.url).host, ...JSON.parse(target.labels || "{}") };
        await store(env, [...parse(await response.text(), extra, now), { name: "up", labels: canonical(extra), value: 1, t: now }]);
      } catch (error) {
        const extra = { instance: new URL(target.url).host, ...JSON.parse(target.labels || "{}") };
        await store(env, [{ name: "up", labels: canonical(extra), value: 0, t: now }]);
      }
    })
  );
  if (new Date(now).getUTCMinutes() === 0) {
    const cutoff = now - Number(env.RETAIN_DAYS || 180) * 86400000;
    await env.DB.prepare("DELETE FROM points WHERE t < ?").bind(cutoff).run();
  }
}

// ---------- alerts ----------
// Every minute the cron evaluates three conditions and posts to ntfy (topic: the NTFY_TOPIC secret) when one starts
// and when it clears. Open alerts live in the D1 table `alerts`, so each fires once.
//   mac-silent        the Mac's heartbeat series (HEARTBEAT on MAC_HOST) has not reported for SILENT_MIN minutes
//   dead:<proj>/<id>  a running node's ops_claim_alive series reports 0 (fresh, for the last 3 minutes)
//   gpu-idle          the heartbeat stayed below IDLE_PCT for IDLE_MIN minutes while a running, queued or ready node needs mac-gpu
// The worker has no copy of the plan, so node state comes from the published site's state.json (STATE_URL).
// SILENT_MIN, IDLE_MIN, IDLE_PCT, MAC_HOST, HEARTBEAT and ALERT_NOTE (a prefix, for tests) are optional vars.
// ntfy.sh limits anonymous publishing per IP and Cloudflare's shared egress IPs hit that daily quota (429), so set
// the NTFY_TOKEN secret (an ntfy.sh access token) for per-account limits. A failed post is retried every minute.
// Messages carry node ids and durations only: ntfy topics are readable by anyone who knows the name.

const MINUTE = 60000;
const OPEN = new Set(["todo", "queued", "running"]);

function setting(env, key, fallback) {
  const value = Number(env[key]);
  return Number.isFinite(value) && env[key] !== undefined && env[key] !== "" ? value : fallback;
}

async function heartbeat(env) {
  const labels = canonical({ host: env.MAC_HOST || "GB-497" });
  return env.DB.prepare("SELECT id, last_t FROM series WHERE name = ? AND labels = ?").bind(env.HEARTBEAT || "gpu_util_percent", labels).first();
}

function ready(nodes, node) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  return (
    ["todo", "queued"].includes(node.status || "todo") &&
    (node.depends || []).every((d) => byId.get(d)?.status === "done") &&
    !nodes.some((child) => child.parent === node.id && OPEN.has(child.status || "todo"))
  );
}

// Returns {active: Map key -> message, judged: prefixes it could evaluate}.
async function conditions(env, now) {
  const active = new Map();
  const judged = ["mac-silent"];
  const silentMin = setting(env, "SILENT_MIN", 30);
  const idleMin = setting(env, "IDLE_MIN", 30);
  const beat = await heartbeat(env);
  if (beat && now - beat.last_t >= silentMin * MINUTE) {
    active.set("mac-silent", `mac silent ${Math.round((now - beat.last_t) / MINUTE)} min (training, collectors or the Mac are down)`);
  }

  let state;
  try {
    const response = await fetch(`${env.STATE_URL || "https://hrishik.me/ops/state.json"}?${now}`, { signal: AbortSignal.timeout(10000) });
    state = await response.json();
  } catch (error) {
    return { active, judged };
  }
  judged.push("dead:", "gpu-idle");
  const projects = Object.entries(state.projects || {});

  const { results: claims } = await env.DB.prepare("SELECT id, labels, last_t, last_v FROM series WHERE name = 'ops_claim_alive' AND last_t >= ?").bind(now - 10 * MINUTE).all();
  for (const row of claims) {
    if (row.last_v !== 0) continue;
    const labels = JSON.parse(row.labels);
    const nodes = state.projects?.[labels.project]?.nodes || [];
    if (!nodes.some((n) => n.id === labels.node && n.status === "running")) continue;
    const recent = await env.DB.prepare("SELECT COUNT(*) AS n, MAX(v) AS top FROM points WHERE series_id = ? AND t >= ?").bind(row.id, now - 3 * MINUTE).first();
    if (recent.n >= 2 && recent.top === 0) active.set(`dead:${labels.project}/${labels.node}`, `${labels.node}: process gone`);
  }

  if (beat) {
    const window = await env.DB.prepare("SELECT COUNT(*) AS n, MAX(v) AS top FROM points WHERE series_id = ? AND t >= ?").bind(beat.id, now - idleMin * MINUTE).first();
    if (window.n >= idleMin / 2 && window.top < setting(env, "IDLE_PCT", 10)) {
      const waiting = { running: [], queued: [], ready: [] };
      for (const [, project] of projects) {
        for (const node of project.nodes || []) {
          if (node.needs !== "mac-gpu") continue;
          if (node.status === "running") waiting.running.push(node.id);
          else if (ready(project.nodes, node)) waiting.ready.push(node.id);
          else if (node.status === "queued") waiting.queued.push(node.id);
        }
      }
      const parts = Object.entries(waiting).filter(([, ids]) => ids.length).map(([kind, ids]) => `${kind}: ${ids.slice(0, 3).join(", ")}${ids.length > 3 ? ", ..." : ""}`);
      if (parts.length) active.set("gpu-idle", `mac-gpu idle ${idleMin}+ min; ${parts.join("; ")}`);
    }
  }
  return { active, judged };
}

async function notify(env, message, recovered) {
  const response = await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
    method: "POST",
    body: env.ALERT_NOTE ? `[${env.ALERT_NOTE}] ${message}` : message,
    headers: {
      Title: recovered ? "ops: recovered" : "ops alert",
      Tags: recovered ? "white_check_mark" : "warning",
      Priority: recovered ? "3" : "4",
      "User-Agent": "ops-worker/1",
      ...(env.NTFY_TOKEN ? { Authorization: `Bearer ${env.NTFY_TOKEN}` } : {}),
    },
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error(`ntfy ${response.status}: ${(await response.text()).slice(0, 200)}`);
}

async function alerts(env, now) {
  if (!env.NTFY_TOPIC) return;
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, message TEXT NOT NULL, since INTEGER NOT NULL)").run();
  const { active, judged } = await conditions(env, now);
  const { results: open } = await env.DB.prepare("SELECT key, message, since FROM alerts").all();
  const known = new Set(open.map((row) => row.key));
  let sent = 0;
  for (const [key, message] of active) {
    if (known.has(key)) continue;
    await notify(env, message, false);
    await env.DB.prepare("INSERT INTO alerts (key, message, since) VALUES (?, ?, ?)").bind(key, message, now).run();
    sent += 1;
  }
  for (const row of open) {
    if (active.has(row.key) || !judged.some((prefix) => row.key.startsWith(prefix))) continue;
    await notify(env, `${row.message}; cleared after ${Math.round((now - row.since) / MINUTE)} min`, true);
    await env.DB.prepare("DELETE FROM alerts WHERE key = ?").bind(row.key).run();
    sent += 1;
  }
  if (sent) {
    const last = await env.DB.prepare("SELECT last_v FROM series WHERE name = 'ops_alerts_sent' AND labels = '{}'").first();
    await store(env, [{ name: "ops_alerts_sent", labels: "{}", value: (last?.last_v || 0) + sent, t: now }]);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return reply("", 204);

    if (url.pathname === "/push" && request.method === "POST") {
      if (!authorized(request, env)) return reply("unauthorized", 401);
      const extra = parseLabels(url.searchParams.get("labels"));
      const count = await store(env, parse(await request.text(), extra, Date.now()));
      return reply({ stored: count });
    }

    if (url.pathname === "/series" && request.method === "DELETE") {
      if (!authorized(request, env)) return reply("unauthorized", 401);
      const ids = (url.searchParams.get("id") || "").split(",").map(Number).filter(Number.isInteger);
      if (!ids.length) return reply("pass ?id=1,2", 400);
      const marks = ids.map(() => "?").join(",");
      await env.DB.batch([
        env.DB.prepare(`DELETE FROM points WHERE series_id IN (${marks})`).bind(...ids),
        env.DB.prepare(`DELETE FROM series WHERE id IN (${marks})`).bind(...ids),
      ]);
      return reply({ deleted: ids.length });
    }

    if (url.pathname === "/series") {
      const q = url.searchParams.get("q");
      const statement = q
        ? env.DB.prepare("SELECT * FROM series WHERE name LIKE ?1 OR labels LIKE ?1 ORDER BY name, labels").bind(`%${q}%`)
        : env.DB.prepare("SELECT * FROM series ORDER BY name, labels");
      const { results } = await statement.all();
      return reply(results.map((row) => ({ ...row, labels: JSON.parse(row.labels) })));
    }

    if (url.pathname === "/points") {
      const ids = (url.searchParams.get("id") || "").split(",").map(Number).filter(Boolean).slice(0, 40);
      const since = Number(url.searchParams.get("since") || 0);
      const max = Math.min(Number(url.searchParams.get("max") || 600), 5000);
      const out = {};
      for (const id of ids) {
        const span = await env.DB.prepare("SELECT MIN(t) AS lo, MAX(t) AS hi, COUNT(*) AS n FROM points WHERE series_id = ? AND t >= ?").bind(id, since).first();
        if (!span || !span.n) {
          out[id] = [];
          continue;
        }
        const statement =
          span.n <= max
            ? env.DB.prepare("SELECT t, v FROM points WHERE series_id = ? AND t >= ? ORDER BY t").bind(id, since)
            : env.DB.prepare("SELECT MAX(t) AS t, AVG(v) AS v FROM points WHERE series_id = ? AND t >= ? GROUP BY (t - ?) / ? ORDER BY t").bind(id, since, span.lo, Math.ceil((span.hi - span.lo + 1) / max));
        out[id] = (await statement.all()).results.map((row) => [row.t, row.v]);
      }
      return reply(out);
    }

    if (url.pathname === "/targets") {
      if (request.method === "GET") return reply((await env.DB.prepare("SELECT * FROM targets").all()).results);
      if (!authorized(request, env)) return reply("unauthorized", 401);
      if (request.method === "POST") {
        const body = await request.json();
        await env.DB.prepare("INSERT OR REPLACE INTO targets (url, labels) VALUES (?, ?)").bind(body.url, JSON.stringify(body.labels || {})).run();
        return reply({ ok: true });
      }
      if (request.method === "DELETE") {
        await env.DB.prepare("DELETE FROM targets WHERE url = ?").bind(url.searchParams.get("url")).run();
        return reply({ ok: true });
      }
    }

    if (url.pathname === "/alerts") {
      const open = await env.DB.prepare("SELECT * FROM alerts").all().catch(() => ({ results: [] }));
      const { active } = await conditions(env, Date.now());
      return reply({ open: open.results, now: Object.fromEntries(active) });
    }

    if (url.pathname === "/") return reply("ops metrics: /push /series /points /targets /alerts");
    return reply("not found", 404);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(scrape(env));
    ctx.waitUntil(alerts(env, Date.now()).catch((error) => console.log(`alerts: ${error}`)));
  },
};
