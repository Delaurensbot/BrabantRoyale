"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type SectionKey = "race" | "clan_stats" | "battles_left";

type Section = {
  title: string;
  text: string;
};

type DashboardData = {
  generated_at_iso: string;
  generated_at_epoch_ms: number;
  update_interval_seconds: number;
  sections: Record<SectionKey, Section>;
  copy_all_text: string;
};

const FETCH_INTERVAL_MS = 1000;
const DEFAULT_INTERVAL_SECONDS = 300;

const skeletonData: DashboardData = {
  generated_at_iso: new Date().toISOString(),
  generated_at_epoch_ms: Date.now(),
  update_interval_seconds: DEFAULT_INTERVAL_SECONDS,
  sections: {
    race: { title: "Race", text: "Loading race data..." },
    clan_stats: { title: "Clan Stats", text: "Loading clan stats..." },
    battles_left: { title: "Battles left (today)", text: "Loading battles..." }
  },
  copy_all_text: "Loading..."
};

function formatCountdown(msRemaining: number): string {
  if (msRemaining <= 0) return "00:00";
  const totalSeconds = Math.floor(msRemaining / 1000);
  const minutes = Math.floor(totalSeconds / 60)
    .toString()
    .padStart(2, "0");
  const seconds = Math.floor(totalSeconds % 60)
    .toString()
    .padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export default function Page() {
  const [data, setData] = useState<DashboardData>(skeletonData);
  const [toast, setToast] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [now, setNow] = useState(Date.now());

  const nextUpdateTs = useMemo(() => {
    const interval = (data?.update_interval_seconds || DEFAULT_INTERVAL_SECONDS) * 1000;
    return (data?.generated_at_epoch_ms || 0) + interval;
  }, [data]);

  const countdown = useMemo(() => formatCountdown(nextUpdateTs - now), [nextUpdateTs, now]);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), FETCH_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);

  const showToast = useCallback((message: string) => {
    setToast(message);
    setTimeout(() => setToast(null), 1400);
  }, []);

  const fetchData = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await fetch(`/data.json?ts=${Date.now()}`);
      if (!res.ok) throw new Error(`Failed to fetch data (${res.status})`);
      const payload = (await res.json()) as DashboardData;
      setData(payload);
    } catch (err) {
      console.error(err);
      showToast("Failed to refresh");
    } finally {
      setIsLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const copyText = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      showToast("Copied!");
    } catch (err) {
      console.error(err);
      showToast("Copy failed");
    }
  }, [showToast]);

  const cards: { key: SectionKey; section: Section }[] = useMemo(
    () => [
      { key: "race", section: data.sections.race },
      { key: "clan_stats", section: data.sections.clan_stats },
      { key: "battles_left", section: data.sections.battles_left }
    ],
    [data.sections]
  );

  const lastUpdated = useMemo(() => {
    try {
      return new Date(data.generated_at_iso).toLocaleString();
    } catch {
      return data.generated_at_iso;
    }
  }, [data.generated_at_iso]);

  return (
    <main className="mx-auto max-w-6xl px-6 py-12 space-y-8">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">CW Stats Dashboard</h1>
          <p className="text-sm text-gray-400">
            Last updated: <span className="text-gray-200 font-medium">{lastUpdated}</span> · Next update in: {countdown}
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <button
            onClick={() => copyText(data.copy_all_text)}
            className="rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-black transition hover:bg-emerald-400"
          >
            Copy all
          </button>
          <button
            onClick={fetchData}
            disabled={isLoading}
            className="rounded-lg border border-gray-600 px-4 py-2 text-sm text-gray-100 hover:border-emerald-500 hover:text-emerald-200 disabled:opacity-60"
          >
            {isLoading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </header>

      <section className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
        {cards.map(({ key, section }) => (
          <button
            key={key}
            onClick={() => copyText(section.text)}
            className="card w-full text-left p-5 focus:outline-none"
          >
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-semibold">{section.title}</h2>
              <span className="text-xs text-gray-400">Click to copy</span>
            </div>
            <div className="mt-3 rounded-lg bg-black/20 p-3 text-sm font-mono text-gray-100 border border-white/5">
              <pre>{section.text || "No data"}</pre>
            </div>
          </button>
        ))}
      </section>

      <div className={`toast ${toast ? "show" : ""}`} aria-live="assertive">{toast || ""}</div>
    </main>
  );
}
