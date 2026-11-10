// ops metrics store: a Cloudflare Worker over D1.
//
// POST /push            text lines `name{k="v",...} value [unix_ms]` (Prometheus text format subset).
//                       Query ?labels=k="v",... adds labels to every line. Needs the bearer token.
// GET  /series          every series with its last value (?q= filters by substring of name or labels).
// DELETE /series?id=1,2 removes those series and all their points (token).
// GET  /points?id=1,2   points per series (?since=unix_ms, ?max=N buckets, averaged).
// GET  /targets         scrape targets; POST {url, labels} adds one; DELETE ?url= removes one (token).
// Cron (every minute)   GETs each target URL and stores what it serves; drops points older than RETAIN_DAYS.

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

    if (url.pathname === "/") return reply("ops metrics: /push /series /points /targets");
    return reply("not found", 404);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(scrape(env));
  },
};
