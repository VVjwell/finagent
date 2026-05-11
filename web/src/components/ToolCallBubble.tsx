import { useState } from "react";

interface Props {
  server: string;
  name: string;
  args: unknown;
  preview?: string;
  error?: string;
}

function formatArgs(args: unknown): string {
  if (args == null) return "";
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

export function ToolCallBubble({ server, name, args, preview, error }: Props) {
  const [open, setOpen] = useState(false);
  const pending = !preview && !error;
  const argsText = formatArgs(args);
  const argsSummary = argsText.length > 80 ? argsText.slice(0, 80) + "…" : argsText;

  return (
    <div className="rounded-lg border border-ink-700/80 bg-ink-900/60 text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-ink-800/60"
      >
        <span
          className={
            "inline-block h-2 w-2 rounded-full " +
            (error
              ? "bg-rose-400"
              : pending
                ? "animate-pulse bg-amber-400"
                : "bg-emerald-400")
          }
        />
        <span className="font-mono text-ink-300">
          <span className="text-ink-500">MCP:</span>
          <span className="text-sky-300">{server}</span>
          <span className="text-ink-500"> · </span>
          <span className="text-ink-100">{name}</span>
        </span>
        {!open && argsSummary && (
          <span className="ml-2 truncate font-mono text-ink-500">{argsSummary}</span>
        )}
        <span className="ml-auto text-ink-500">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-ink-700/80 px-3 py-2 font-mono">
          <div>
            <div className="mb-0.5 text-[10px] uppercase tracking-wide text-ink-500">
              args
            </div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words text-ink-200">
              {argsText || "(无)"}
            </pre>
          </div>
          <div>
            <div className="mb-0.5 text-[10px] uppercase tracking-wide text-ink-500">
              {error ? "error" : preview ? "result preview" : "waiting…"}
            </div>
            {error ? (
              <pre className="overflow-x-auto whitespace-pre-wrap break-words text-rose-300">
                {error}
              </pre>
            ) : preview ? (
              <pre className="overflow-x-auto whitespace-pre-wrap break-words text-ink-200">
                {preview}
              </pre>
            ) : (
              <div className="italic text-ink-500">调用中…</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
