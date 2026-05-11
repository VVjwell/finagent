import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AssistantSegment } from "../types";
import { ToolCallBubble } from "./ToolCallBubble";

interface Props {
  segments: AssistantSegment[];
  streaming: boolean;
}

export function AssistantBubble({ segments, streaming }: Props) {
  return (
    <div className="flex justify-start">
      <div className="max-w-[88%] space-y-2 rounded-2xl rounded-tl-md bg-ink-800/70 px-4 py-2.5 text-sm text-ink-100 ring-1 ring-ink-700/60">
        {segments.length === 0 && streaming && (
          <span className="inline-block h-3 w-2 animate-pulse bg-ink-400" />
        )}
        {segments.map((seg, i) => {
          if (seg.kind === "text") {
            if (!seg.text) return null;
            return (
              <div key={i} className="markdown-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{seg.text}</ReactMarkdown>
              </div>
            );
          }
          return (
            <ToolCallBubble
              key={i}
              server={seg.server}
              name={seg.name}
              args={seg.args}
              preview={seg.preview}
              error={seg.error}
            />
          );
        })}
        {streaming && segments.some((s) => s.kind === "text" && s.text) && (
          <span className="inline-block h-3 w-2 translate-y-0.5 animate-pulse bg-ink-400" />
        )}
      </div>
    </div>
  );
}
