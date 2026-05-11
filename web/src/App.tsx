import { useEffect } from "react";
import { ChatWindow } from "./components/ChatWindow";
import { Sidebar } from "./components/Sidebar";
import { useChatSocket } from "./hooks/useChatSocket";
import { api } from "./lib/api";
import { useChatStore } from "./store/chatStore";

export default function App() {
  const threadId = useChatStore((s) => s.threadId);
  const resetForThread = useChatStore((s) => s.resetForThread);
  const setErrorBanner = useChatStore((s) => s.setErrorBanner);

  // 启动时自动建一个新会话（除非 URL 上带了 thread）
  useEffect(() => {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("thread");
    if (fromUrl) {
      resetForThread(fromUrl);
      return;
    }
    (async () => {
      try {
        const r = await api.createSession();
        resetForThread(r.thread_id);
      } catch (e) {
        setErrorBanner(
          `后端未连上：${(e as Error).message}。请确认 uvicorn server.main:app 已启动。`,
        );
      }
    })();
  }, [resetForThread, setErrorBanner]);

  // thread 切换时同步 URL
  useEffect(() => {
    if (!threadId) return;
    const url = new URL(window.location.href);
    if (url.searchParams.get("thread") !== threadId) {
      url.searchParams.set("thread", threadId);
      window.history.replaceState({}, "", url.toString());
    }
  }, [threadId]);

  const socket = useChatSocket(threadId);

  return (
    <div className="flex h-screen w-screen bg-ink-950 text-ink-100">
      <Sidebar />
      <ChatWindow socket={socket} />
    </div>
  );
}
