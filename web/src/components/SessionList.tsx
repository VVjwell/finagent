import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useChatStore } from "../store/chatStore";
import type { ChatMessage } from "../types";

export function SessionList() {
  const threadId = useChatStore((s) => s.threadId);
  const resetForThread = useChatStore((s) => s.resetForThread);
  const setMessages = useChatStore((s) => s.setMessages);
  const [threads, setThreads] = useState<string[]>([]);

  const refresh = async () => {
    try {
      const r = await api.listSessions();
      setThreads(r.threads);
    } catch {
      /* 后端没起的时候静默 */
    }
  };

  useEffect(() => {
    refresh();
  }, [threadId]);

  const newSession = async () => {
    const r = await api.createSession();
    resetForThread(r.thread_id);
    setMessages([]);
  };

  const switchTo = async (tid: string) => {
    if (tid === threadId) return;
    resetForThread(tid);
    // 拉历史
    try {
      const h = await api.getMessages(tid);
      // 把后端的 type=human/ai 直接映射到 user/assistant
      // 工具调用/工具结果先粗暴忽略（历史的工具气泡需要更细的合并逻辑，留给后续）
      const msgs: ChatMessage[] = h.messages
        .filter((m) => m.type === "human" || m.type === "ai")
        .filter((m) => m.content && m.content.trim())
        .map((m, i) => {
          const role: "user" | "assistant" =
            m.type === "human" ? "user" : "assistant";
          return {
            id: `hist-${tid}-${i}`,
            role,
            text: role === "user" ? m.content : undefined,
            segments:
              role === "assistant"
                ? [{ kind: "text", text: m.content }]
                : undefined,
            done: true,
            ts: Date.now(),
          };
        });
      setMessages(msgs);
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-ink-800 px-3 py-2">
        <span className="text-xs uppercase tracking-wide text-ink-400">
          Sessions
        </span>
        <button
          onClick={newSession}
          className="rounded-md bg-ink-800 px-2 py-1 text-xs text-ink-100 hover:bg-ink-700"
        >
          + 新会话
        </button>
      </div>
      <div className="flex-1 space-y-0.5 overflow-y-auto p-2">
        {threads.length === 0 && (
          <div className="px-2 py-4 text-xs text-ink-500">没有会话，点上方新建</div>
        )}
        {threads.map((tid) => (
          <button
            key={tid}
            onClick={() => switchTo(tid)}
            className={
              "block w-full truncate rounded-md px-2 py-1.5 text-left font-mono text-xs " +
              (tid === threadId
                ? "bg-sky-500/15 text-sky-200 ring-1 ring-sky-500/30"
                : "text-ink-300 hover:bg-ink-800")
            }
          >
            {tid}
          </button>
        ))}
      </div>
    </div>
  );
}
