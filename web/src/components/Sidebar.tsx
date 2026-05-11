import { useState } from "react";
import { ReportsPanel } from "./ReportsPanel";
import { SessionList } from "./SessionList";

type Tab = "sessions" | "reports";

export function Sidebar() {
  const [tab, setTab] = useState<Tab>("sessions");

  return (
    <aside className="flex h-full w-72 flex-col border-r border-ink-800 bg-ink-950/40">
      <div className="flex items-center border-b border-ink-800">
        {(["sessions", "reports"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={
              "flex-1 px-3 py-2 text-xs uppercase tracking-wide " +
              (tab === t
                ? "border-b-2 border-sky-400 text-ink-100"
                : "text-ink-500 hover:text-ink-200")
            }
          >
            {t}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-hidden">
        {tab === "sessions" ? <SessionList /> : <ReportsPanel />}
      </div>
      <div className="border-t border-ink-800 px-3 py-2 text-[10px] text-ink-500">
        finAgent · CLI 仍可用 <span className="font-mono">python cli.py</span>
      </div>
    </aside>
  );
}
