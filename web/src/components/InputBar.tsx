import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type { ChatSocket } from "../hooks/useChatSocket";
import { useChatStore } from "../store/chatStore";

interface Props {
  socket: ChatSocket;
}

const HEARTBEAT_IDLE_MS = 5 * 60 * 1000;

export function InputBar({ socket }: Props) {
  const [text, setText] = useState("");
  const status = useChatStore((s) => s.status);
  const appendUserLocal = useChatStore((s) => s.appendUserLocal);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // 高度自适应
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [text]);

  // idle heartbeat：用户停手 5 分钟自动让 agent 主动开口
  // 注意：仅在 open 状态下计时；streaming/connecting 不算 idle
  useEffect(() => {
    if (status !== "open") return;
    const t = window.setTimeout(() => {
      socket.send({ kind: "heartbeat" });
    }, HEARTBEAT_IDLE_MS);
    return () => window.clearTimeout(t);
  }, [status, socket, text]);

  const busy = status === "streaming" || status === "connecting";

  const submit = () => {
    const t = text.trim();
    if (!t || busy) return;
    appendUserLocal(t);
    socket.send({ kind: "user", text: t });
    setText("");
  };

  const handleKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="border-t border-ink-800 bg-ink-950/60 px-6 py-4 backdrop-blur">
      <div className="mx-auto flex max-w-3xl items-end gap-3">
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKey}
          placeholder={
            busy
              ? "agent 正在回话…"
              : "和 finAgent 聊点啥（Enter 发送，Shift+Enter 换行）"
          }
          disabled={busy}
          rows={1}
          className="flex-1 resize-none rounded-xl bg-ink-900 px-4 py-2.5 text-sm leading-relaxed text-ink-100 placeholder-ink-500 outline-none ring-1 ring-ink-700/60 focus:ring-sky-500/60 disabled:opacity-50"
        />
        <button
          onClick={submit}
          disabled={!text.trim() || busy}
          className="rounded-xl bg-sky-500 px-4 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:bg-ink-700 disabled:text-ink-400"
        >
          发送
        </button>
      </div>
    </div>
  );
}
