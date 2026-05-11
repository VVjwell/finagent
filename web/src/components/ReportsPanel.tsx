import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../lib/api";
import type { ReportItem } from "../types";

type Kind = "daily" | "journal" | "root";

const KIND_LABELS: Record<Kind, string> = {
  daily: "Daily",
  journal: "Journal",
  root: "Reports",
};

export function ReportsPanel() {
  const [kind, setKind] = useState<Kind>("daily");
  const [items, setItems] = useState<ReportItem[]>([]);
  const [openName, setOpenName] = useState<string | null>(null);
  const [openContent, setOpenContent] = useState<string>("");
  const [loadingDoc, setLoadingDoc] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.listReports(kind);
        if (!cancelled) {
          setItems(r.items);
          setOpenName(null);
          setOpenContent("");
        }
      } catch {
        if (!cancelled) setItems([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [kind]);

  const open = async (name: string) => {
    setOpenName(name);
    setLoadingDoc(true);
    try {
      const r = await api.readReport(kind, name);
      setOpenContent(r.content);
    } catch {
      setOpenContent("[读取失败]");
    } finally {
      setLoadingDoc(false);
    }
  };

  const list = useMemo(
    () => (
      <div className="flex-1 space-y-0.5 overflow-y-auto p-2">
        {items.length === 0 && (
          <div className="px-2 py-4 text-xs text-ink-500">这一类还没有文件</div>
        )}
        {items.map((it) => (
          <button
            key={it.name}
            onClick={() => open(it.name)}
            className={
              "block w-full truncate rounded-md px-2 py-1.5 text-left text-xs " +
              (it.name === openName
                ? "bg-sky-500/15 text-sky-200 ring-1 ring-sky-500/30"
                : "text-ink-300 hover:bg-ink-800")
            }
            title={new Date(it.modified * 1000).toLocaleString()}
          >
            {it.name}
          </button>
        ))}
      </div>
    ),
    [items, openName],
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-1 border-b border-ink-800 px-2 py-2">
        {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
          <button
            key={k}
            onClick={() => setKind(k)}
            className={
              "rounded-md px-2 py-1 text-xs " +
              (k === kind
                ? "bg-ink-800 text-ink-100"
                : "text-ink-400 hover:bg-ink-800/60")
            }
          >
            {KIND_LABELS[k]}
          </button>
        ))}
      </div>
      {list}
      {openName && (
        <div className="border-t border-ink-800 bg-ink-900/40 p-3">
          <div className="mb-2 flex items-center gap-2 text-xs">
            <span className="font-mono text-ink-300">{openName}</span>
            <button
              onClick={() => {
                setOpenName(null);
                setOpenContent("");
              }}
              className="ml-auto text-ink-500 hover:text-ink-200"
            >
              关闭
            </button>
          </div>
          <div className="markdown-body max-h-[40vh] overflow-y-auto text-xs">
            {loadingDoc ? (
              <div className="text-ink-500">加载中…</div>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{openContent}</ReactMarkdown>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
