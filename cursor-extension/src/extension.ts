import * as vscode from "vscode";
import * as http from "http";

let server: http.Server | null = null;
// Terminal output buffer: terminal name -> last N lines
const terminalOutput: Map<string, string[]> = new Map();
const OUTPUT_BUFFER_LINES = 200;

interface TerminalInfo {
  id: number;
  name: string;
  pid: number | undefined;
  isActive: boolean;
}

function getConfig() {
  const cfg = vscode.workspace.getConfiguration("khalil.terminalBridge");
  return {
    port: cfg.get<number>("port", 8034),
    authToken: cfg.get<string>("authToken", ""),
  };
}

function authenticate(req: http.IncomingMessage): boolean {
  const { authToken } = getConfig();
  if (!authToken) return true; // no token configured = open (localhost only)
  const header = req.headers["authorization"];
  return header === `Bearer ${authToken}`;
}

function jsonResponse(
  res: http.ServerResponse,
  status: number,
  body: unknown
) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks).toString()));
    req.on("error", reject);
  });
}

async function listTerminals(): Promise<TerminalInfo[]> {
  const active = vscode.window.activeTerminal;
  const results: TerminalInfo[] = [];
  for (const t of vscode.window.terminals) {
    let pid: number | undefined;
    try {
      pid = await t.processId;
    } catch {
      pid = undefined;
    }
    results.push({
      id: results.length,
      name: t.name,
      pid,
      isActive: t === active,
    });
  }
  return results;
}

function findTerminal(
  nameOrIndex: string | number
): vscode.Terminal | undefined {
  const terminals = vscode.window.terminals;
  if (typeof nameOrIndex === "number") {
    return terminals[nameOrIndex];
  }
  // Try exact name match first, then partial
  return (
    terminals.find((t) => t.name === nameOrIndex) ||
    terminals.find((t) =>
      t.name.toLowerCase().includes(String(nameOrIndex).toLowerCase())
    )
  );
}

async function handleRequest(
  req: http.IncomingMessage,
  res: http.ServerResponse
) {
  // CORS for local dev
  res.setHeader("Access-Control-Allow-Origin", "http://localhost:*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  if (!authenticate(req)) {
    return jsonResponse(res, 401, { error: "Unauthorized" });
  }

  const url = new URL(req.url || "/", `http://localhost`);
  const path = url.pathname;

  try {
    // GET /status — health check
    if (path === "/status" && req.method === "GET") {
      return jsonResponse(res, 200, {
        ok: true,
        terminals: vscode.window.terminals.length,
        workspace: vscode.workspace.workspaceFolders?.[0]?.name || null,
      });
    }

    // GET /terminals — list all terminals
    if (path === "/terminals" && req.method === "GET") {
      const terminals = await listTerminals();
      return jsonResponse(res, 200, { terminals });
    }

    // POST /terminals — create a new terminal
    if (path === "/terminals" && req.method === "POST") {
      const body = JSON.parse(await readBody(req));
      const name = body.name || "Khalil";
      const cwd = body.cwd || undefined;
      const terminal = vscode.window.createTerminal({
        name,
        cwd,
      });
      terminal.show(body.preserveFocus ?? true);
      if (body.command) {
        terminal.sendText(body.command);
      }
      return jsonResponse(res, 201, {
        name: terminal.name,
        message: "Terminal created",
      });
    }

    // POST /terminals/:target/send — send text to terminal
    const sendMatch = path.match(/^\/terminals\/(.+)\/send$/);
    if (sendMatch && req.method === "POST") {
      const target = decodeURIComponent(sendMatch[1]);
      const idx = parseInt(target, 10);
      const terminal = findTerminal(isNaN(idx) ? target : idx);
      if (!terminal) {
        return jsonResponse(res, 404, { error: `Terminal not found: ${target}` });
      }
      const body = JSON.parse(await readBody(req));
      const text = body.text || body.command;
      if (!text) {
        return jsonResponse(res, 400, { error: "Missing 'text' or 'command' field" });
      }
      terminal.sendText(text, body.addNewLine ?? true);
      if (body.show) {
        terminal.show(body.preserveFocus ?? true);
      }
      return jsonResponse(res, 200, {
        sent: true,
        terminal: terminal.name,
        text,
      });
    }

    // DELETE /terminals/:target — close a terminal
    const deleteMatch = path.match(/^\/terminals\/(.+)$/);
    if (
      deleteMatch &&
      req.method === "DELETE" &&
      !path.includes("/send")
    ) {
      const target = decodeURIComponent(deleteMatch[1]);
      const idx = parseInt(target, 10);
      const terminal = findTerminal(isNaN(idx) ? target : idx);
      if (!terminal) {
        return jsonResponse(res, 404, { error: `Terminal not found: ${target}` });
      }
      const name = terminal.name;
      terminal.dispose();
      return jsonResponse(res, 200, { closed: true, terminal: name });
    }

    // POST /terminals/:target/show — focus a terminal
    const showMatch = path.match(/^\/terminals\/(.+)\/show$/);
    if (showMatch && req.method === "POST") {
      const target = decodeURIComponent(showMatch[1]);
      const idx = parseInt(target, 10);
      const terminal = findTerminal(isNaN(idx) ? target : idx);
      if (!terminal) {
        return jsonResponse(res, 404, { error: `Terminal not found: ${target}` });
      }
      terminal.show(false);
      return jsonResponse(res, 200, { shown: true, terminal: terminal.name });
    }

    // GET /output/:target — get buffered output for a terminal
    const outputMatch = path.match(/^\/output\/(.+)$/);
    if (outputMatch && req.method === "GET") {
      const target = decodeURIComponent(outputMatch[1]);
      const lines = url.searchParams.get("lines");
      const limit = lines ? parseInt(lines, 10) : 50;
      const buffer = terminalOutput.get(target);
      if (!buffer) {
        return jsonResponse(res, 200, { terminal: target, output: [], note: "No output captured yet" });
      }
      return jsonResponse(res, 200, {
        terminal: target,
        output: buffer.slice(-limit),
      });
    }

    // GET /workspace — current workspace info
    if (path === "/workspace" && req.method === "GET") {
      const folders = vscode.workspace.workspaceFolders?.map((f) => ({
        name: f.name,
        path: f.uri.fsPath,
      })) || [];
      const editor = vscode.window.activeTextEditor;
      return jsonResponse(res, 200, {
        folders,
        activeFile: editor
          ? {
              path: editor.document.uri.fsPath,
              language: editor.document.languageId,
              line: editor.selection.active.line + 1,
            }
          : null,
      });
    }

    jsonResponse(res, 404, { error: "Not found", path });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    jsonResponse(res, 500, { error: message });
  }
}

function startServer(ctx: vscode.ExtensionContext) {
  if (server) {
    vscode.window.showInformationMessage("Khalil Terminal Bridge already running");
    return;
  }

  const { port } = getConfig();

  server = http.createServer(handleRequest);
  server.listen(port, "127.0.0.1", () => {
    vscode.window.showInformationMessage(
      `Khalil Terminal Bridge listening on http://127.0.0.1:${port}`
    );
  });

  server.on("error", (err: NodeJS.ErrnoException) => {
    if (err.code === "EADDRINUSE") {
      vscode.window.showErrorMessage(
        `Khalil Terminal Bridge: port ${port} already in use`
      );
    } else {
      vscode.window.showErrorMessage(
        `Khalil Terminal Bridge error: ${err.message}`
      );
    }
    server = null;
  });

  // Capture terminal output via onDidWriteTerminalData (proposed API)
  // Falls back gracefully if not available
  try {
    const onWrite = (vscode.window as any).onDidWriteTerminalData;
    if (onWrite) {
      const disposable = onWrite((e: { terminal: vscode.Terminal; data: string }) => {
        const name = e.terminal.name;
        if (!terminalOutput.has(name)) {
          terminalOutput.set(name, []);
        }
        const buffer = terminalOutput.get(name)!;
        // Split by newlines and append
        const lines = e.data.split(/\r?\n/);
        buffer.push(...lines.filter((l: string) => l.length > 0));
        // Trim to max buffer size
        while (buffer.length > OUTPUT_BUFFER_LINES) {
          buffer.shift();
        }
      });
      ctx.subscriptions.push(disposable);
    }
  } catch {
    // Proposed API not available — output capture disabled
  }
}

function stopServer() {
  if (server) {
    server.close();
    server = null;
    vscode.window.showInformationMessage("Khalil Terminal Bridge stopped");
  }
}

export function activate(ctx: vscode.ExtensionContext) {
  ctx.subscriptions.push(
    vscode.commands.registerCommand("khalil.terminalBridge.start", () =>
      startServer(ctx)
    ),
    vscode.commands.registerCommand("khalil.terminalBridge.stop", () =>
      stopServer()
    )
  );

  // Auto-start on activation
  startServer(ctx);

  // Clean up terminal output buffers when terminals close
  ctx.subscriptions.push(
    vscode.window.onDidCloseTerminal((t) => {
      terminalOutput.delete(t.name);
    })
  );
}

export function deactivate() {
  stopServer();
  terminalOutput.clear();
}
