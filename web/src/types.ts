// 后端 WebSocket 推过来的事件类型。跟 utils/agent_core.py 和 utils/mcp_tools.py 对齐。

export type ServerEvent =
  | { type: "user"; text: string }
  | { type: "assistant_start" }
  | { type: "token"; text: string }
  | { type: "turn_break" }
  | { type: "done"; had_content: boolean }
  | { type: "tool_call"; server: string; name: string; args: unknown }
  | { type: "tool_result"; server: string; name: string; preview: string }
  | { type: "tool_error"; server: string; name: string; error: string }
  | { type: "error"; error: string };

// 前端渲染用的"逻辑消息"：把流式 token / turn_break / tool_* 折叠成一条消息的片段列表
export type AssistantSegment =
  | { kind: "text"; text: string }
  | { kind: "tool"; server: string; name: string; args: unknown; preview?: string; error?: string };

export interface ChatMessage {
  id: string;
  // user / assistant / system
  role: "user" | "assistant" | "system";
  // user 消息直接用 text，assistant 用 segments 拼
  text?: string;
  segments?: AssistantSegment[];
  // assistant 在流式过程中 done=false；done 后才能进入下一轮
  done: boolean;
  ts: number;
}

export interface SessionInfo {
  thread_id: string;
}

export interface ReportItem {
  name: string;
  size: number;
  modified: number;
}
