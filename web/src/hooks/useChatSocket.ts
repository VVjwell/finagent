import { useEffect, useRef } from "react";
import { wsUrl } from "../lib/api";
import { useChatStore } from "../store/chatStore";
import type { ServerEvent } from "../types";

export interface ChatSocket {
  send: (msg: { kind: "user" | "kickoff" | "heartbeat" | "reflect"; text?: string }) => void;
}

/**
 * 给定 threadId 时建立 WS 连接，断了自动重连一次。
 * 消费 server 推过来的事件转发给 chatStore.applyEvent。
 */
export function useChatSocket(threadId: string | null): ChatSocket {
  const wsRef = useRef<WebSocket | null>(null);
  const setStatus = useChatStore((s) => s.setStatus);
  const applyEvent = useChatStore((s) => s.applyEvent);
  const setErrorBanner = useChatStore((s) => s.setErrorBanner);

  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    let retried = false;

    const connect = () => {
      if (cancelled) return;
      setStatus("connecting");
      const ws = new WebSocket(wsUrl(threadId));
      wsRef.current = ws;
      ws.onopen = () => setStatus("open");
      ws.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data) as ServerEvent;
          applyEvent(ev);
        } catch {
          setErrorBanner("收到无法解析的事件");
        }
      };
      ws.onerror = () => setStatus("error");
      ws.onclose = () => {
        if (cancelled) return;
        setStatus("closed");
        if (!retried) {
          retried = true;
          setTimeout(connect, 800);
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [threadId, setStatus, applyEvent, setErrorBanner]);

  return {
    send: (msg) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        setErrorBanner("连接未就绪，请稍候再发");
        return;
      }
      ws.send(JSON.stringify(msg));
    },
  };
}
