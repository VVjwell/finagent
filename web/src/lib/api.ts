import type { ReportItem } from "../types";

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

async function jpost<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export const api = {
  createSession: () => jpost<{ thread_id: string }>("/api/sessions"),
  listSessions: () => jget<{ threads: string[] }>("/api/sessions"),
  getMessages: (tid: string) =>
    jget<{ messages: Array<{ type: string; content: string; tool_calls?: unknown }> }>(
      `/api/sessions/${tid}/messages`,
    ),
  listReports: (kind: "daily" | "journal" | "root") =>
    jget<{ items: ReportItem[] }>(`/api/reports/${kind}`),
  readReport: (kind: "daily" | "journal" | "root", name: string) =>
    jget<{ name: string; content: string }>(
      `/api/reports/${kind}/${encodeURIComponent(name)}`,
    ),
  getPrompt: (kind: "kickoff" | "heartbeat" | "reflection") =>
    jget<{ text: string }>(`/api/prompts/${kind}`),
  health: () => jget<{ ok: boolean; tools: number }>("/api/health"),
};

export function wsUrl(threadId: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/sessions/${threadId}`;
}
