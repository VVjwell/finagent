import { useEffect, useRef } from "react";
import { useChatStore } from "../store/chatStore";
import { AssistantBubble } from "./AssistantBubble";
import { UserBubble } from "./UserBubble";

export function MessageList() {
  const messages = useChatStore((s) => s.messages);
  const status = useChatStore((s) => s.status);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 自动滚到底，但只在用户没主动往上看时；判定方式：距底 < 200px 才滚
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distFromBottom < 200 || status === "streaming") {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages, status]);

  return (
    <div
      ref={scrollRef}
      className="flex-1 space-y-4 overflow-y-auto px-6 py-6"
    >
      {messages.length === 0 && (
        <div className="mx-auto max-w-md py-16 text-center text-sm text-ink-500">
          这里是 finAgent。
          <br />
          输入一条消息开始，或者点上方"启动问候"让它先开口。
        </div>
      )}
      {messages.map((m) =>
        m.role === "user" ? (
          <UserBubble key={m.id} text={m.text ?? ""} />
        ) : (
          <AssistantBubble
            key={m.id}
            segments={m.segments ?? []}
            streaming={!m.done}
          />
        ),
      )}
    </div>
  );
}
