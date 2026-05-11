import type { ChatSocket } from "../hooks/useChatSocket";
import { useChatStore } from "../store/chatStore";

interface Props {
  socket: ChatSocket;
}

const STATUS_TEXT: Record<string, { label: string; color: string }> = {
  idle: { label: "未连接", color: "bg-ink-500" },
  connecting: { label: "连接中", color: "bg-amber-400 animate-pulse" },
  open: { label: "在线", color: "bg-emerald-400" },
  streaming: { label: "回话中", color: "bg-sky-400 animate-pulse" },
  closed: { label: "已断开", color: "bg-ink-500" },
  error: { label: "异常", color: "bg-rose-400" },
};

export function TopBar({ socket }: Props) {
  const threadId = useChatStore((s) => s.threadId);
  const status = useChatStore((s) => s.status);
  const errorBanner = useChatStore((s) => s.errorBanner);
  const setErrorBanner = useChatStore((s) => s.setErrorBanner);
  const s = STATUS_TEXT[status] ?? STATUS_TEXT.idle;

  return (
    <div className="border-b border-ink-800 bg-ink-950/60 backdrop-blur">
      <div className="flex h-12 items-center gap-3 px-5">
        <span className="text-base font-medium text-ink-100">finAgent</span>
        <span className="font-mono text-xs text-ink-500">
          thread: {threadId ?? "—"}
        </span>
        <span className="ml-2 flex items-center gap-1.5 text-xs text-ink-400">
          <span className={"inline-block h-2 w-2 rounded-full " + s.color} />
          {s.label}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => socket.send({ kind: "kickoff" })}
            disabled={status !== "open"}
            className="rounded-md bg-ink-800 px-2.5 py-1 text-xs text-ink-100 hover:bg-ink-700 disabled:cursor-not-allowed disabled:opacity-50"
            title="让 agent 主动开口（kickoff 模板）"
          >
            启动问候
          </button>
          <button
            onClick={() => socket.send({ kind: "heartbeat" })}
            disabled={status !== "open"}
            className="rounded-md bg-ink-800 px-2.5 py-1 text-xs text-ink-100 hover:bg-ink-700 disabled:cursor-not-allowed disabled:opacity-50"
            title="手动触发 heartbeat"
          >
            戳一下
          </button>
          <button
            onClick={() => socket.send({ kind: "reflect" })}
            disabled={status !== "open"}
            className="rounded-md bg-ink-800 px-2.5 py-1 text-xs text-ink-100 hover:bg-ink-700 disabled:cursor-not-allowed disabled:opacity-50"
            title="触发收尾反思"
          >
            收尾反思
          </button>
        </div>
      </div>
      {errorBanner && (
        <div className="flex items-center gap-2 bg-rose-900/30 px-5 py-1.5 text-xs text-rose-200">
          <span className="font-mono">error</span>
          <span className="flex-1 truncate">{errorBanner}</span>
          <button
            onClick={() => setErrorBanner(null)}
            className="text-rose-300/80 hover:text-rose-100"
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}
