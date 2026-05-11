import { create } from "zustand";
import type { AssistantSegment, ChatMessage, ServerEvent } from "../types";

type Status = "idle" | "connecting" | "open" | "streaming" | "closed" | "error";

interface ChatState {
  threadId: string | null;
  status: Status;
  messages: ChatMessage[];
  errorBanner: string | null;

  setThread: (tid: string | null) => void;
  setStatus: (s: Status) => void;
  setMessages: (m: ChatMessage[]) => void;
  resetForThread: (tid: string) => void;
  applyEvent: (ev: ServerEvent) => void;
  appendUserLocal: (text: string) => void;
  setErrorBanner: (text: string | null) => void;
}

// 生成 16 位字符串 id，浏览器原生 randomUUID 兜底 Math.random
function mkid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return Math.random().toString(36).slice(2);
}

function lastAssistantOrCreate(state: ChatState): ChatMessage {
  const last = state.messages[state.messages.length - 1];
  if (last && last.role === "assistant" && !last.done) return last;
  const m: ChatMessage = {
    id: mkid(),
    role: "assistant",
    segments: [],
    done: false,
    ts: Date.now(),
  };
  state.messages = [...state.messages, m];
  return m;
}

function appendTextSegment(msg: ChatMessage, text: string) {
  const segs = msg.segments ?? [];
  const last = segs[segs.length - 1];
  if (last && last.kind === "text") {
    last.text += text;
  } else {
    segs.push({ kind: "text", text });
  }
  msg.segments = segs;
}

export const useChatStore = create<ChatState>((set, get) => ({
  threadId: null,
  status: "idle",
  messages: [],
  errorBanner: null,

  setThread: (tid) => set({ threadId: tid }),
  setStatus: (s) => set({ status: s }),
  setMessages: (m) => set({ messages: m }),
  setErrorBanner: (text) => set({ errorBanner: text }),

  resetForThread: (tid) =>
    set({ threadId: tid, messages: [], status: "connecting", errorBanner: null }),

  appendUserLocal: (text) => {
    const m: ChatMessage = {
      id: mkid(),
      role: "user",
      text,
      done: true,
      ts: Date.now(),
    };
    set({ messages: [...get().messages, m] });
  },

  applyEvent: (ev) => {
    set((state) => {
      const next: ChatState = { ...state, messages: [...state.messages] };
      switch (ev.type) {
        case "user": {
          // 服务端确认用户消息，前端已经乐观插入一条 user，跳过避免重复
          // 但万一前端没插（kickoff/heartbeat 后端注入），这里补一条
          const last = next.messages[next.messages.length - 1];
          if (!last || last.role !== "user" || last.text !== ev.text) {
            next.messages.push({
              id: mkid(),
              role: "user",
              text: ev.text,
              done: true,
              ts: Date.now(),
            });
          }
          break;
        }
        case "assistant_start": {
          // 创建一条空 assistant 消息，等 token 填
          next.messages.push({
            id: mkid(),
            role: "assistant",
            segments: [],
            done: false,
            ts: Date.now(),
          });
          next.status = "streaming";
          break;
        }
        case "token": {
          const m = lastAssistantOrCreate(next);
          appendTextSegment(m, ev.text);
          // 替换数组里那一条（zustand 浅比较）
          next.messages = next.messages.map((x) => (x.id === m.id ? { ...m } : x));
          break;
        }
        case "turn_break": {
          // 工具调用回归后的段落分隔：插入一个空 text 段强制分块
          const m = lastAssistantOrCreate(next);
          const segs = m.segments ?? [];
          segs.push({ kind: "text", text: "" });
          m.segments = segs;
          next.messages = next.messages.map((x) => (x.id === m.id ? { ...m } : x));
          break;
        }
        case "tool_call": {
          const m = lastAssistantOrCreate(next);
          const segs = m.segments ?? [];
          segs.push({
            kind: "tool",
            server: ev.server,
            name: ev.name,
            args: ev.args,
          });
          m.segments = segs;
          next.messages = next.messages.map((x) => (x.id === m.id ? { ...m } : x));
          break;
        }
        case "tool_result": {
          const m = lastAssistantOrCreate(next);
          const segs = m.segments ?? [];
          // 回填最近一个同名 tool 段的 preview
          for (let i = segs.length - 1; i >= 0; i--) {
            const s = segs[i];
            if (s.kind === "tool" && s.name === ev.name && !s.preview && !s.error) {
              const updated: AssistantSegment = { ...s, preview: ev.preview };
              segs[i] = updated;
              break;
            }
          }
          m.segments = segs;
          next.messages = next.messages.map((x) => (x.id === m.id ? { ...m } : x));
          break;
        }
        case "tool_error": {
          const m = lastAssistantOrCreate(next);
          const segs = m.segments ?? [];
          for (let i = segs.length - 1; i >= 0; i--) {
            const s = segs[i];
            if (s.kind === "tool" && s.name === ev.name && !s.preview && !s.error) {
              const updated: AssistantSegment = { ...s, error: ev.error };
              segs[i] = updated;
              break;
            }
          }
          m.segments = segs;
          next.messages = next.messages.map((x) => (x.id === m.id ? { ...m } : x));
          break;
        }
        case "done": {
          // 把最后一条 assistant 标记 done；如果完全没内容（had_content=false），干脆删掉
          const idx = next.messages.length - 1;
          if (idx >= 0 && next.messages[idx].role === "assistant") {
            const m = { ...next.messages[idx], done: true };
            const hasAny = (m.segments ?? []).some(
              (s) => (s.kind === "text" && s.text.length) || s.kind === "tool",
            );
            if (!ev.had_content && !hasAny) {
              next.messages = next.messages.slice(0, idx);
            } else {
              next.messages[idx] = m;
            }
          }
          next.status = "open";
          break;
        }
        case "error": {
          next.errorBanner = ev.error;
          break;
        }
      }
      return next;
    });
  },
}));
