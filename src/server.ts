/**
 * server.ts — claude-tmux webui HTTP server (Bun.serve).
 *
 * Binds to 127.0.0.1 only (localhost single-user; pane content + transcripts are
 * sensitive — never bind 0.0.0.0). Builds the React SPA via Bun.build and serves it,
 * plus GET /api/topology from the live tmux topology.
 */

import { watch } from "node:fs";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { getTopology } from "./topology";

const WEB_DIR = join(import.meta.dir, "web");
const DIST_DIR = join(WEB_DIR, "dist");

export interface ServeOptions {
  port?: number;
  host?: string;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

async function buildBundle(): Promise<boolean> {
  const result = await Bun.build({
    entrypoints: [join(WEB_DIR, "index.tsx")],
    outdir: DIST_DIR,
    minify: false,
    sourcemap: "inline",
    target: "browser",
  });
  if (!result.success) {
    console.error("[build] failed:", result.logs);
    return false;
  }
  return true;
}

async function handleStatic(path: string): Promise<Response> {
  if (path === "/" || path === "/index.html") {
    const html = await readFile(join(WEB_DIR, "index.html"), "utf-8");
    return new Response(html, { headers: { "Content-Type": "text/html" } });
  }
  if (path === "/index.js") {
    try {
      const js = await readFile(join(DIST_DIR, "index.js"), "utf-8");
      return new Response(js, {
        headers: {
          "Content-Type": "application/javascript",
          "Cache-Control": "no-cache, no-store, must-revalidate",
        },
      });
    } catch {
      return new Response("// bundle not built", { status: 404 });
    }
  }
  if (path === "/favicon.svg" || path === "/favicon.ico") {
    try {
      const svg = await readFile(join(WEB_DIR, "favicon.svg"), "utf-8");
      return new Response(svg, { headers: { "Content-Type": "image/svg+xml" } });
    } catch {
      return new Response("", { status: 404 });
    }
  }
  return new Response("Not found", { status: 404 });
}

export async function startServer(opts: ServeOptions = {}) {
  const port = opts.port ?? 8810;
  const host = opts.host ?? "127.0.0.1";

  if (!(await buildBundle())) {
    throw new Error("web build failed");
  }

  // Dev hot-rebuild: rebuild bundle on web source change.
  let t: ReturnType<typeof setTimeout> | null = null;
  try {
    watch(WEB_DIR, { recursive: true }, (_e, file) => {
      if (file && /\.(tsx?|css|html)$/.test(file) && !file.includes("dist")) {
        if (t) clearTimeout(t);
        t = setTimeout(() => {
          console.log(`[hot-reload] ${file} → rebuilding`);
          buildBundle();
        }, 300);
      }
    });
  } catch {
    /* watch optional */
  }

  const server = Bun.serve({
    port,
    hostname: host,
    async fetch(req) {
      const path = new URL(req.url).pathname;
      try {
        if (path === "/api/topology") return json(await getTopology());
        if (path === "/api/health") return json({ ok: true });
        return await handleStatic(path);
      } catch (err) {
        console.error("request error:", err);
        return json({ error: err instanceof Error ? err.message : "internal error" }, 500);
      }
    },
  });

  console.log(`claude-tmux webui → http://${host}:${port}`);
  return server;
}
