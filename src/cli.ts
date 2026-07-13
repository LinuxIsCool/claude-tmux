#!/usr/bin/env bun
/**
 * cli.ts — claude-tmux webui CLI.
 *
 *   bun run src/cli.ts serve [--port 8810] [--host 127.0.0.1] [--open]
 *   bun run src/cli.ts topology        # print topology JSON and exit
 */

import { getTopology } from "./topology";
import { startServer } from "./server";

function arg(name: string, fallback?: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const has = (name: string) => process.argv.includes(`--${name}`);

const cmd = process.argv[2] ?? "serve";

if (cmd === "topology") {
  console.log(JSON.stringify(await getTopology(), null, 2));
  process.exit(0);
}

if (cmd === "serve") {
  const port = Number.parseInt(arg("port", "8810")!, 10);
  const host = arg("host", "127.0.0.1")!;
  await startServer({ port, host });
  if (has("open")) {
    const url = `http://${host}:${port}`;
    Bun.spawn(["xdg-open", url], { stdout: "ignore", stderr: "ignore" });
  }
  // keep process alive
} else {
  console.error(`unknown command: ${cmd}\nusage: cli.ts [serve|topology] [--port N] [--host H] [--open]`);
  process.exit(1);
}
