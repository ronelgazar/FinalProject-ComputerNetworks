import React, { useEffect, useMemo, useRef, useState } from "react";

type Phase = "boot" | "connecting" | "syncing" | "waiting" | "running" | "submitted" | "error";

type Question =
  | { id: string; type: "mcq"; prompt: string; options: { id: string; text: string }[]; points?: number }
  | { id: string; type: "text"; prompt: string; placeholder?: string; points?: number };

type ExamPayload = {
  exam_id: string;
  title: string;
  instructions: string;
  duration_sec: number;
  questions: Question[];
};

type Answer =
  | { qid: string; type: "mcq"; optionId: string | null }
  | { qid: string; type: "text"; text: string };

type SyncSample = { rtt: number; offset: number; at: number };

// ── Clock helpers ────────────────────────────────────────────────────────────
function nowMs() {
  return performance.timeOrigin + performance.now();
}
function clamp(n: number, a: number, b: number) {
  return Math.max(a, Math.min(b, n));
}
function serverTimeFromLocal(localMs: number, offsetMs: number) {
  return localMs + offsetMs;
}
function formatTime(ms: number) {
  const d = new Date(ms);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms3 = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms3}`;
}
function formatCountdown(ms: number) {
  const sign = ms < 0 ? "-" : "";
  const abs = Math.abs(ms);
  const totalSec = Math.floor(abs / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${sign}${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${sign}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function formatDuration(ms: number) {
  const sign = ms < 0 ? "-" : "";
  const abs = Math.abs(ms);
  const totalSec = Math.floor(abs / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  const msR = abs % 1000;
  if (m > 0) return `${sign}${m}ד ${String(s).padStart(2, "0")}ש`;
  if (totalSec > 0) return `${sign}${s}ש`;
  return `${sign}${msR.toFixed(0)}ms`;
}
function timeAgo(tMs: number) {
  const d = nowMs() - tMs;
  if (d < 1000) return "עכשיו";
  const s = Math.floor(d / 1000);
  if (s < 60) return `לפני ${s}ש`;
  const m = Math.floor(s / 60);
  return `לפני ${m}ד`;
}

// ── WS RPC ───────────────────────────────────────────────────────────────────
type WSMessage =
  | { type: "hello"; client_id: string }
  | { type: "time_req"; req_id: string; client_t0_ms: number }
  | { type: "time_resp"; req_id: string; server_now_ms: number }
  | { type: "schedule_req"; req_id: string }
  | { type: "schedule_resp"; req_id: string; exam_id: string; start_at_server_ms: number; duration_sec: number }
  | { type: "exam_req"; req_id: string; exam_id: string }
  | { type: "exam_resp"; req_id: string; exam: ExamPayload }
  | { type: "answers_save"; req_id: string; exam_id: string; answers: Answer[] }
  | { type: "answers_saved"; req_id: string; ok: true }
  | { type: "submit_req"; req_id: string; exam_id: string }
  | { type: "submit_resp"; req_id: string; ok: true }
  | { type: "error"; req_id?: string; message: string };

function rid() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

class WSClient {
  private ws: WebSocket | null = null;
  private pending = new Map<string, { resolve: (v: any) => void; reject: (e: any) => void; t: number }>();
  private onEvent?: (msg: WSMessage) => void;
  private url: string;

  constructor(url: string) { this.url = url; }

  connect(onEvent?: (msg: WSMessage) => void) {
    this.onEvent = onEvent;
    return new Promise<void>((resolve, reject) => {
      try {
        this.ws = new WebSocket(this.url);
        this.ws.onopen = () => resolve();
        this.ws.onerror = () => reject(new Error("שגיאה בחיבור Socket"));
        this.ws.onmessage = (ev) => {
          let msg: WSMessage;
          try { msg = JSON.parse(ev.data); } catch { return; }
          const anyMsg: any = msg as any;
          const reqId = anyMsg.req_id as string | undefined;
          if (reqId && this.pending.has(reqId)) {
            const p = this.pending.get(reqId)!;
            this.pending.delete(reqId);
            if (msg.type === "error") p.reject(new Error(msg.message));
            else p.resolve(msg);
            return;
          }
          this.onEvent?.(msg);
        };
        this.ws.onclose = () => {
          for (const [k, p] of this.pending) {
            p.reject(new Error("החיבור נסגר"));
            this.pending.delete(k);
          }
        };
      } catch (e) { reject(e); }
    });
  }

  close() { this.ws?.close(); this.ws = null; }

  send(msg: WSMessage) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) throw new Error("Socket לא מחובר");
    this.ws.send(JSON.stringify(msg));
  }

  request<TResp extends WSMessage>(msg: WSMessage, timeoutMs = 4000): Promise<TResp> {
    const anyMsg: any = msg as any;
    const req_id = anyMsg.req_id as string | undefined;
    if (!req_id) throw new Error("Missing req_id");
    this.send(msg);
    return new Promise<TResp>((resolve, reject) => {
      const t = window.setTimeout(() => {
        if (this.pending.has(req_id)) {
          this.pending.delete(req_id);
          reject(new Error("Timeout"));
        }
      }, timeoutMs);
      this.pending.set(req_id, {
        t: t as unknown as number,
        resolve: (v) => { window.clearTimeout(t); resolve(v as TResp); },
        reject: (e) => { window.clearTimeout(t); reject(e); },
      });
    });
  }
}

const WS_URL = import.meta.env.VITE_WS_URL || `ws://${window.location.host}/ws`;

// ── CSS injection ─────────────────────────────────────────────────────────────
function GlobalStyles() {
  return (
    <style>{`
      *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
      body { background: #f1f5f9; font-family: 'Segoe UI', system-ui, -apple-system, Arial, sans-serif; }
      button { font-family: inherit; cursor: pointer; }
      textarea { font-family: inherit; }
      input { font-family: inherit; }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }
      @keyframes fade-in {
        from { opacity: 0; transform: translateY(8px); }
        to   { opacity: 1; transform: translateY(0); }
      }
      @keyframes pulse-red {
        0%, 100% { color: #dc2626; }
        50%       { color: #f87171; }
      }
      @keyframes pulse-ring {
        0%   { box-shadow: 0 0 0 0 rgba(37,99,235,0.35); }
        70%  { box-shadow: 0 0 0 18px rgba(37,99,235,0); }
        100% { box-shadow: 0 0 0 0 rgba(37,99,235,0); }
      }
      @keyframes tick {
        0%  { transform: scale(1); }
        5%  { transform: scale(1.04); }
        10% { transform: scale(1); }
      }

      .fade-in   { animation: fade-in 0.3s ease both; }
      .spinner   { width: 40px; height: 40px; border: 3px solid #e2e8f0;
                   border-top-color: #2563eb; border-radius: 50%;
                   animation: spin 0.7s linear infinite; }
      .spinner-sm{ width: 16px; height: 16px; border: 2px solid #e2e8f0;
                   border-top-color: #2563eb; border-radius: 50%;
                   animation: spin 0.7s linear infinite; }
      .tick-anim { animation: tick 1s steps(1) infinite; }

      /* MCQ option hover */
      .mcq-opt:hover { border-color: #93c5fd !important; background: #f0f9ff !important; }
      .mcq-opt.selected { border-color: #2563eb !important; background: #eff6ff !important; }

      /* Q nav button */
      .q-nav-btn:hover { opacity: 0.85; }

      /* Scrollbar */
      ::-webkit-scrollbar { width: 6px; }
      ::-webkit-scrollbar-track { background: transparent; }
      ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 99px; }
    `}</style>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [phase, setPhase] = useState<Phase>("boot");
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WSClient | null>(null);

  const [syncSamples, setSyncSamples] = useState<SyncSample[]>([]);
  const bestSample = useMemo(() => {
    if (syncSamples.length === 0) return null;
    return [...syncSamples].sort((a, b) => a.rtt - b.rtt)[0];
  }, [syncSamples]);
  const offsetMs = bestSample?.offset ?? 0;
  const [syncQuality, setSyncQuality] = useState<"לא ידוע" | "טוב" | "בינוני" | "חלש">("לא ידוע");

  const [examId, setExamId] = useState<string | null>(null);
  const [startAtServerMs, setStartAtServerMs] = useState<number | null>(null);
  const [durationSec, setDurationSec] = useState<number | null>(null);

  const [exam, setExam] = useState<ExamPayload | null>(null);
  const [activeQ, setActiveQ] = useState<number>(0);
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [saving, setSaving] = useState<boolean>(false);
  const [lastSavedAt, setLastSavedAt] = useState<number | null>(null);
  const autosaveTimer = useRef<number | null>(null);

  const [localNow, setLocalNow] = useState<number>(() => nowMs());
  useEffect(() => {
    const t = window.setInterval(() => setLocalNow(nowMs()), 100);
    return () => window.clearInterval(t);
  }, []);
  const serverNow = useMemo(() => serverTimeFromLocal(localNow, offsetMs), [localNow, offsetMs]);

  const msToStart = useMemo(() => {
    if (!startAtServerMs) return null;
    return startAtServerMs - serverNow;
  }, [startAtServerMs, serverNow]);

  const msLeft = useMemo(() => {
    if (phase !== "running" || !startAtServerMs || !durationSec) return null;
    return startAtServerMs + durationSec * 1000 - serverNow;
  }, [phase, startAtServerMs, durationSec, serverNow]);

  // ── Boot: connect → sync → schedule → waiting ────────────────────────────
  useEffect(() => {
    let alive = true;
    async function run() {
      try {
        setPhase("connecting");
        const ws = new WSClient(WS_URL);
        wsRef.current = ws;
        await ws.connect();
        if (!alive) return;

        ws.send({ type: "hello", client_id: `web-${Math.random().toString(36).slice(2, 8)}` });
        setPhase("syncing");

        const samples: SyncSample[] = [];
        const rounds = 12;
        for (let i = 0; i < rounds; i++) {
          const req_id = rid();
          const t0 = nowMs();
          const resp = await ws.request<{ type: "time_resp"; req_id: string; server_now_ms: number }>(
            { type: "time_req", req_id, client_t0_ms: t0 }, 2500
          );
          const t1 = nowMs();
          const rtt = t1 - t0;
          const offset = resp.server_now_ms - (t0 + rtt / 2);
          samples.push({ rtt, offset, at: t1 });
          setSyncSamples([...samples]);
          await new Promise((r) => setTimeout(r, 120));
        }

        const best = [...samples].sort((a, b) => a.rtt - b.rtt)[0];
        if (best.rtt < 40) setSyncQuality("טוב");
        else if (best.rtt < 120) setSyncQuality("בינוני");
        else setSyncQuality("חלש");

        const sched = await ws.request<{
          type: "schedule_resp"; req_id: string; exam_id: string;
          start_at_server_ms: number; duration_sec: number;
        }>({ type: "schedule_req", req_id: rid() }, 4000);

        setExamId(sched.exam_id);
        setStartAtServerMs(sched.start_at_server_ms);
        setDurationSec(sched.duration_sec);
        setPhase("waiting");
      } catch (e: any) {
        setError(e?.message ?? String(e));
        setPhase("error");
      }
    }
    run();
    return () => { alive = false; wsRef.current?.close(); wsRef.current = null; };
  }, []);

  // ── waiting → running ────────────────────────────────────────────────────
  useEffect(() => {
    if (phase !== "waiting") return;
    if (!examId || !startAtServerMs) return;
    const localStart = startAtServerMs - offsetMs;
    const delay = clamp(localStart - nowMs(), 0, 60_000);
    const t = window.setTimeout(async () => {
      try {
        const ws = wsRef.current;
        if (!ws) throw new Error("Socket לא זמין");
        setPhase("running");
        const exResp = await ws.request<{ type: "exam_resp"; req_id: string; exam: ExamPayload }>(
          { type: "exam_req", req_id: rid(), exam_id: examId }, 6000
        );
        setExam(exResp.exam);
        const init: Record<string, Answer> = {};
        for (const q of exResp.exam.questions) {
          init[q.id] = q.type === "mcq"
            ? { qid: q.id, type: "mcq", optionId: null }
            : { qid: q.id, type: "text", text: "" };
        }
        setAnswers(init);
      } catch (e: any) {
        setError(e?.message ?? String(e));
        setPhase("error");
      }
    }, delay);
    return () => window.clearTimeout(t);
  }, [phase, examId, startAtServerMs, offsetMs]);

  // ── Autosave ─────────────────────────────────────────────────────────────
  useEffect(() => {
    if (phase !== "running" || !examId) return;
    if (autosaveTimer.current) window.clearInterval(autosaveTimer.current);
    autosaveTimer.current = window.setInterval(async () => {
      try {
        const ws = wsRef.current;
        if (!ws) return;
        setSaving(true);
        await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
          { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) }, 4000
        );
        setLastSavedAt(nowMs());
      } catch { /* soft-fail */ } finally { setSaving(false); }
    }, 10_000);
    return () => { if (autosaveTimer.current) window.clearInterval(autosaveTimer.current); autosaveTimer.current = null; };
  }, [phase, examId, answers]);

  // ── Auto-submit on time up ────────────────────────────────────────────────
  useEffect(() => {
    if (phase !== "running" || msLeft == null || msLeft > 0) return;
    (async () => {
      try {
        const ws = wsRef.current;
        if (!ws || !examId) return;
        setSaving(true);
        await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
          { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) }, 4000
        );
        await ws.request<{ type: "submit_resp"; req_id: string; ok: true }>(
          { type: "submit_req", req_id: rid(), exam_id: examId }, 5000
        );
        setPhase("submitted");
      } catch (e: any) {
        setError(e?.message ?? String(e)); setPhase("error");
      } finally { setSaving(false); }
    })();
  }, [phase, msLeft, examId, answers]);

  // ── Answered count ────────────────────────────────────────────────────────
  const answeredCount = useMemo(() => Object.values(answers).filter(a =>
    a.type === "mcq" ? a.optionId != null : a.text.trim().length > 0
  ).length, [answers]);

  // ── Save handler ──────────────────────────────────────────────────────────
  const handleSave = async () => {
    const ws = wsRef.current;
    if (!ws || !examId) return;
    setSaving(true);
    try {
      await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
        { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) }, 4000
      );
      setLastSavedAt(nowMs());
    } finally { setSaving(false); }
  };

  const handleSubmit = async () => {
    const ws = wsRef.current;
    if (!ws || !examId) return;
    if (!confirm("להגיש את הבחינה? לאחר ההגשה לא ניתן לערוך.")) return;
    setSaving(true);
    try {
      await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
        { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) }, 4000
      );
      await ws.request<{ type: "submit_resp"; req_id: string; ok: true }>(
        { type: "submit_req", req_id: rid(), exam_id: examId }, 5000
      );
      setPhase("submitted");
    } catch (e: any) {
      setError(e?.message ?? String(e)); setPhase("error");
    } finally { setSaving(false); }
  };

  // ── Sync quality ──────────────────────────────────────────────────────────
  const syncBadge = {
    "טוב":    { bg: "#dcfce7", color: "#16a34a", dot: "#22c55e" },
    "בינוני": { bg: "#fef3c7", color: "#d97706", dot: "#f59e0b" },
    "חלש":    { bg: "#fee2e2", color: "#dc2626", dot: "#ef4444" },
    "לא ידוע":{ bg: "#f1f5f9", color: "#64748b", dot: "#94a3b8" },
  }[syncQuality];

  const isLowTime = msLeft !== null && msLeft > 0 && msLeft < 5 * 60 * 1000;
  const isUrgent  = msLeft !== null && msLeft > 0 && msLeft < 60 * 1000;

  // ── Top bar ───────────────────────────────────────────────────────────────
  const topBar = (
    <header style={s.topBar}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={s.logo}>🎓</div>
        <div>
          <div style={{ fontWeight: 700, fontSize: 15, color: "#0f172a" }}>מערכת הבחינה</div>
          <div style={{ fontSize: 12, color: "#64748b", fontFamily: "monospace" }}>
            {formatTime(serverNow)}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {/* Sync badge */}
        {phase !== "boot" && phase !== "connecting" && (
          <div style={{ ...s.badge, background: syncBadge.bg, color: syncBadge.color }}>
            <div style={{ width: 7, height: 7, borderRadius: "50%", background: syncBadge.dot }} />
            סנכרון {syncQuality}
          </div>
        )}

        {/* Timer */}
        {phase === "waiting" && msToStart != null && msToStart > 0 && (
          <div style={{ ...s.timerPill, background: "#eff6ff", color: "#2563eb", border: "1.5px solid #bfdbfe" }}>
            ⏱ מתחיל בעוד {formatCountdown(msToStart)}
          </div>
        )}
        {phase === "running" && msLeft != null && msLeft > 0 && (
          <div style={{
            ...s.timerPill,
            background: isUrgent ? "#fee2e2" : isLowTime ? "#fff7ed" : "#f0fdf4",
            color:      isUrgent ? "#dc2626" : isLowTime ? "#ea580c" : "#16a34a",
            border:     `1.5px solid ${isUrgent ? "#fca5a5" : isLowTime ? "#fed7aa" : "#bbf7d0"}`,
            animation:  isUrgent ? "pulse-red 1s ease-in-out infinite" : "none",
          }}>
            ⏱ {formatCountdown(msLeft)}
          </div>
        )}

        {/* Save status */}
        {phase === "running" && (
          <div style={{ fontSize: 12, color: "#64748b", display: "flex", alignItems: "center", gap: 6 }}>
            {saving
              ? <><div className="spinner-sm" /> שומר…</>
              : lastSavedAt
                ? <span style={{ color: "#16a34a" }}>✓ {timeAgo(lastSavedAt)}</span>
                : <span>לא נשמר עדיין</span>
            }
          </div>
        )}

        {/* Progress */}
        {phase === "running" && exam && (
          <div style={{ fontSize: 12, color: "#64748b" }}>
            {answeredCount}/{exam.questions.length} נענו
          </div>
        )}
      </div>
    </header>
  );

  return (
    <>
      <GlobalStyles />
      <div style={s.page}>
        {topBar}
        <main style={s.main}>
          {phase === "connecting" && <ConnectingScreen url={WS_URL} />}
          {phase === "syncing"    && <SyncPanel samples={syncSamples} total={12} />}
          {phase === "waiting"    && (
            <WaitingRoom
              examId={examId}
              startAtServerMs={startAtServerMs}
              durationSec={durationSec}
              serverNow={serverNow}
              offsetMs={offsetMs}
              bestSample={bestSample}
              msToStart={msToStart}
            />
          )}
          {phase === "running" && exam && (
            <ExamView
              exam={exam}
              activeQ={activeQ}
              setActiveQ={setActiveQ}
              answers={answers}
              setAnswers={setAnswers}
              onSave={handleSave}
              onSubmit={handleSubmit}
              msLeft={msLeft}
              answeredCount={answeredCount}
              saving={saving}
            />
          )}
          {phase === "submitted" && <SubmittedScreen />}
          {phase === "error"     && <ErrorScreen error={error ?? "שגיאה לא ידועה"} url={WS_URL} />}
        </main>
      </div>
    </>
  );
}

// ── Connecting ────────────────────────────────────────────────────────────────
function ConnectingScreen({ url }: { url: string }) {
  return (
    <div className="fade-in" style={s.centerCard}>
      <div className="spinner" />
      <div style={{ fontWeight: 700, fontSize: 18, color: "#0f172a", marginTop: 20 }}>מתחבר לשרת…</div>
      <div style={{ fontSize: 13, color: "#64748b", marginTop: 6, fontFamily: "monospace" }}>{url}</div>
    </div>
  );
}

// ── Sync ──────────────────────────────────────────────────────────────────────
function SyncPanel({ samples, total }: { samples: SyncSample[]; total: number }) {
  const best = useMemo(() => samples.length ? [...samples].sort((a, b) => a.rtt - b.rtt)[0] : null, [samples]);
  const pct = Math.round((samples.length / total) * 100);

  return (
    <div className="fade-in" style={{ ...s.centerCard, maxWidth: 480 }}>
      <div style={{ fontSize: 28, marginBottom: 8 }}>📡</div>
      <div style={{ fontWeight: 700, fontSize: 18, color: "#0f172a" }}>סנכרון שעון מול השרת</div>
      <div style={{ fontSize: 13, color: "#64748b", marginTop: 4, marginBottom: 20 }}>
        {samples.length} / {total} דגימות
      </div>

      {/* Progress bar */}
      <div style={{ width: "100%", background: "#e2e8f0", borderRadius: 99, height: 8, marginBottom: 20 }}>
        <div style={{
          width: `${pct}%`, height: 8, borderRadius: 99,
          background: "linear-gradient(90deg, #2563eb, #60a5fa)",
          transition: "width 0.3s ease",
        }} />
      </div>

      {/* Stats */}
      <div style={{ display: "flex", gap: 12, width: "100%" }}>
        <StatBox label="RTT מינימלי" value={best ? `${best.rtt.toFixed(1)} ms` : "—"} />
        <StatBox label="הפרש שעון" value={best ? `${best.offset.toFixed(1)} ms` : "—"} />
        <StatBox label="דגימות" value={`${samples.length}`} />
      </div>

      {/* Sample bars */}
      {samples.length > 0 && (
        <div style={{ width: "100%", marginTop: 16, display: "flex", gap: 3, alignItems: "flex-end", height: 40 }}>
          {samples.map((s, i) => {
            const maxRtt = Math.max(...samples.map(x => x.rtt), 1);
            const h = Math.max(4, (s.rtt / maxRtt) * 40);
            const isBest = s === best;
            return (
              <div key={i} style={{
                flex: 1, height: h, borderRadius: 3,
                background: isBest ? "#2563eb" : "#bfdbfe",
                title: `RTT: ${s.rtt.toFixed(1)}ms`,
              }} />
            );
          })}
        </div>
      )}
      <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>
        עמודה כחולה כהה = דגימה בשימוש (RTT מינימלי)
      </div>
    </div>
  );
}

// ── Waiting room ──────────────────────────────────────────────────────────────
function WaitingRoom({
  examId, startAtServerMs, durationSec, serverNow, offsetMs, bestSample, msToStart,
}: {
  examId: string | null; startAtServerMs: number | null; durationSec: number | null;
  serverNow: number; offsetMs: number; bestSample: SyncSample | null; msToStart: number | null;
}) {
  const totalSec = durationSec ?? 0;
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const durationLabel = h > 0 ? `${h}:${String(m).padStart(2, "0")} שעות` : `${m} דקות`;

  return (
    <div className="fade-in" style={{ display: "grid", gap: 16, maxWidth: 580, margin: "0 auto" }}>

      {/* Big countdown */}
      <div style={{ ...s.card, textAlign: "center", padding: "36px 24px" }}>
        <div style={{ fontSize: 13, color: "#64748b", marginBottom: 8, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em" }}>
          הבחינה מתחילה בעוד
        </div>
        <div className="tick-anim" style={{
          fontSize: 72, fontWeight: 800, letterSpacing: -2,
          color: "#0f172a", fontFamily: "ui-monospace, monospace",
          lineHeight: 1,
        }}>
          {msToStart != null && msToStart > 0 ? formatCountdown(msToStart) : "00:00"}
        </div>
        <div style={{ fontSize: 13, color: "#94a3b8", marginTop: 8 }}>דקות : שניות</div>

        <div style={{
          marginTop: 24, padding: "12px 20px",
          background: "#eff6ff", borderRadius: 10,
          color: "#1d4ed8", fontSize: 13, fontWeight: 600,
        }}>
          ✓ כל הנבחנים יתחילו בדיוק באותו הרגע — מסונכרן לשעון השרת
        </div>
      </div>

      {/* Details */}
      <div style={{ ...s.card, display: "grid", gap: 14 }}>
        <div style={{ fontWeight: 700, fontSize: 14, color: "#0f172a" }}>פרטי הבחינה</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <StatBox label="מזהה בחינה" value={examId ?? "—"} />
          <StatBox label="משך הבחינה" value={durationSec != null ? durationLabel : "—"} />
          <StatBox label="התחלה (שרת)" value={startAtServerMs ? formatTime(startAtServerMs).slice(0, 8) : "—"} />
          <StatBox label="RTT מינימלי" value={bestSample ? `${bestSample.rtt.toFixed(1)} ms` : "—"} />
        </div>

        <div style={s.divider} />

        <div style={{ fontSize: 12, color: "#64748b", lineHeight: 1.7 }}>
          <strong>הפרש שעון מחושב:</strong> {bestSample ? `${bestSample.offset.toFixed(1)} ms` : "—"} &nbsp;·&nbsp;
          <strong>התחלה מקומית:</strong>{" "}
          {startAtServerMs ? formatTime(startAtServerMs - offsetMs).slice(0, 8) : "—"}
        </div>

        <div style={{
          padding: 12, borderRadius: 8,
          background: "#fff7ed", border: "1px solid #fed7aa",
          fontSize: 12, color: "#92400e",
        }}>
          ⚠ אין לסגור את הדפדפן. גם אם יש עיכוב ברשת — ההתחלה מתוזמנת לשעון המקומי המחושב.
        </div>
      </div>
    </div>
  );
}

// ── Exam view ─────────────────────────────────────────────────────────────────
function ExamView({
  exam, activeQ, setActiveQ, answers, setAnswers, onSave, onSubmit, msLeft, answeredCount, saving,
}: {
  exam: ExamPayload; activeQ: number; setActiveQ: (n: number) => void;
  answers: Record<string, Answer>;
  setAnswers: React.Dispatch<React.SetStateAction<Record<string, Answer>>>;
  onSave: () => Promise<void>;
  onSubmit: () => Promise<void>;
  msLeft: number | null; answeredCount: number; saving: boolean;
}) {
  const q = exam.questions[activeQ];
  const total = exam.questions.length;
  const isLast = activeQ === total - 1;
  const isFirst = activeQ === 0;

  return (
    <div className="fade-in" style={s.examGrid}>
      {/* ── Main question panel ── */}
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>

        {/* Question header */}
        <div style={{ ...s.card, padding: "14px 18px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={s.qBadge}>{activeQ + 1}</div>
              <div>
                <div style={{ fontWeight: 700, fontSize: 15, color: "#0f172a" }}>
                  שאלה {activeQ + 1} מתוך {total}
                </div>
                <div style={{ fontSize: 12, color: "#64748b" }}>
                  {q.type === "mcq" ? "רב-ברירה" : "שאלה פתוחה"}
                  {q.points != null ? ` · ${q.points} נקודות` : ""}
                </div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button style={s.btnOutline} onClick={onSave} disabled={saving}>
                {saving ? <><div className="spinner-sm" style={{ display: "inline-block" }} /> שומר</> : "💾 שמירה"}
              </button>
            </div>
          </div>

          {/* Inline progress bar */}
          <div style={{ marginTop: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#94a3b8", marginBottom: 4 }}>
              <span>שאלה {activeQ + 1} מתוך {total}</span>
              <span>{answeredCount}/{total} נענו</span>
            </div>
            <div style={{ background: "#e2e8f0", borderRadius: 99, height: 5 }}>
              <div style={{
                width: `${(activeQ / Math.max(total - 1, 1)) * 100}%`,
                height: 5, borderRadius: 99,
                background: "linear-gradient(90deg, #2563eb, #60a5fa)",
                transition: "width 0.3s",
              }} />
            </div>
          </div>
        </div>

        {/* Question body */}
        <div style={s.card}>
          <div style={{ fontWeight: 700, fontSize: 17, color: "#0f172a", lineHeight: 1.5, marginBottom: 18 }}>
            {q.prompt}
          </div>

          {q.type === "mcq" ? (
            <div style={{ display: "grid", gap: 10 }}>
              {q.options.map((opt) => {
                const cur = answers[q.id] as Answer | undefined;
                const selected = cur?.type === "mcq" && cur.optionId === opt.id;
                return (
                  <div
                    key={opt.id}
                    className={`mcq-opt${selected ? " selected" : ""}`}
                    style={{
                      display: "flex", alignItems: "center", gap: 14,
                      padding: "14px 16px", borderRadius: 10, cursor: "pointer",
                      border: selected ? "2px solid #2563eb" : "1.5px solid #e2e8f0",
                      background: selected ? "#eff6ff" : "#fff",
                      transition: "all 0.15s",
                      userSelect: "none",
                    }}
                    onClick={() => setAnswers(prev => ({ ...prev, [q.id]: { qid: q.id, type: "mcq", optionId: opt.id } }))}
                  >
                    <div style={{
                      width: 22, height: 22, borderRadius: "50%", flexShrink: 0,
                      border: selected ? "7px solid #2563eb" : "2px solid #cbd5e1",
                      background: "white",
                      transition: "border 0.15s",
                    }} />
                    <span style={{ fontSize: 15, color: selected ? "#1e40af" : "#1e293b", fontWeight: selected ? 600 : 400 }}>
                      {opt.text}
                    </span>
                    {selected && <span style={{ marginRight: "auto", color: "#2563eb", fontSize: 18 }}>✓</span>}
                  </div>
                );
              })}
              {(answers[q.id] as any)?.optionId != null && (
                <button
                  style={s.btnGhost}
                  onClick={() => setAnswers(prev => ({ ...prev, [q.id]: { qid: q.id, type: "mcq", optionId: null } }))}
                >
                  ✕ ניקוי בחירה
                </button>
              )}
            </div>
          ) : (
            <textarea
              style={s.textarea}
              placeholder={q.placeholder ?? "הקלידו את תשובתכם כאן…"}
              value={(answers[q.id] as any)?.text ?? ""}
              onChange={(e) => setAnswers(prev => ({ ...prev, [q.id]: { qid: q.id, type: "text", text: e.target.value } }))}
            />
          )}
        </div>

        {/* Prev / Next */}
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
          <button style={s.btnOutline} disabled={isFirst} onClick={() => setActiveQ(activeQ - 1)}>
            ← הקודמת
          </button>
          <button
            style={{ ...s.btnPrimary, opacity: isLast ? 0.5 : 1 }}
            disabled={isLast}
            onClick={() => setActiveQ(activeQ + 1)}
          >
            הבאה →
          </button>
        </div>
      </div>

      {/* ── Sidebar ── */}
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>

        {/* Timer card */}
        {msLeft != null && msLeft > 0 && (
          <div style={{
            ...s.card, textAlign: "center", padding: "16px 12px",
            background: msLeft < 60_000 ? "#fef2f2" : msLeft < 300_000 ? "#fff7ed" : "#f0fdf4",
          }}>
            <div style={{ fontSize: 11, color: "#64748b", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              זמן שנותר
            </div>
            <div style={{
              fontSize: 38, fontWeight: 800, fontFamily: "ui-monospace, monospace",
              color: msLeft < 60_000 ? "#dc2626" : msLeft < 300_000 ? "#ea580c" : "#16a34a",
              animation: msLeft < 60_000 ? "pulse-red 1s ease-in-out infinite" : "none",
            }}>
              {formatCountdown(msLeft)}
            </div>
          </div>
        )}

        {/* Question nav grid */}
        <div style={s.card}>
          <div style={{ fontWeight: 700, fontSize: 13, color: "#0f172a", marginBottom: 12 }}>
            ניווט שאלות
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 8 }}>
            {exam.questions.map((qq, idx) => {
              const a = answers[qq.id];
              const answered = a?.type === "mcq" ? a.optionId != null : a?.type === "text" ? a.text.trim().length > 0 : false;
              const isCurrent = idx === activeQ;
              return (
                <button
                  key={qq.id}
                  className="q-nav-btn"
                  style={{
                    width: "100%", aspectRatio: "1", borderRadius: 8,
                    border: isCurrent ? "2px solid #2563eb" : "1.5px solid #e2e8f0",
                    background: isCurrent ? "#2563eb" : answered ? "#dcfce7" : "#f8fafc",
                    color: isCurrent ? "#fff" : answered ? "#16a34a" : "#64748b",
                    fontWeight: 700, fontSize: 13,
                    cursor: "pointer", transition: "all 0.15s",
                  }}
                  title={`שאלה ${idx + 1}${answered ? " (נענתה)" : ""}`}
                  onClick={() => setActiveQ(idx)}
                >
                  {idx + 1}
                </button>
              );
            })}
          </div>

          {/* Legend */}
          <div style={{ display: "flex", gap: 12, marginTop: 12, fontSize: 11, color: "#64748b" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <div style={{ width: 10, height: 10, borderRadius: 3, background: "#2563eb" }} /> נוכחי
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <div style={{ width: 10, height: 10, borderRadius: 3, background: "#dcfce7", border: "1px solid #bbf7d0" }} /> נענה
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <div style={{ width: 10, height: 10, borderRadius: 3, background: "#f8fafc", border: "1px solid #e2e8f0" }} /> ריק
            </div>
          </div>
        </div>

        {/* Summary */}
        <div style={s.card}>
          <div style={{ fontWeight: 700, fontSize: 13, color: "#0f172a", marginBottom: 10 }}>
            סיכום
          </div>
          <div style={{ display: "grid", gap: 8 }}>
            <div style={s.summaryRow}>
              <span style={{ color: "#64748b" }}>נענו</span>
              <span style={{ fontWeight: 700, color: "#16a34a" }}>{answeredCount} / {total}</span>
            </div>
            <div style={s.summaryRow}>
              <span style={{ color: "#64748b" }}>לא נענו</span>
              <span style={{ fontWeight: 700, color: total - answeredCount > 0 ? "#ea580c" : "#94a3b8" }}>
                {total - answeredCount}
              </span>
            </div>
          </div>
          <div style={{ marginTop: 12, background: "#e2e8f0", borderRadius: 99, height: 8 }}>
            <div style={{
              width: `${(answeredCount / total) * 100}%`, height: 8, borderRadius: 99,
              background: answeredCount === total ? "#16a34a" : "#2563eb",
              transition: "width 0.4s",
            }} />
          </div>
        </div>

        {/* Submit */}
        <button
          style={{ ...s.btnSubmit }}
          onClick={onSubmit}
          disabled={saving}
        >
          {saving ? "מגיש…" : "הגשת הבחינה"}
        </button>

        {answeredCount < total && (
          <div style={{ fontSize: 11, color: "#94a3b8", textAlign: "center" }}>
            {total - answeredCount} שאלות עדיין ריקות. ניתן להגיש בכל מקרה.
          </div>
        )}
      </div>
    </div>
  );
}

// ── Submitted ─────────────────────────────────────────────────────────────────
function SubmittedScreen() {
  return (
    <div className="fade-in" style={{ ...s.centerCard, maxWidth: 480 }}>
      <div style={{ fontSize: 64 }}>✅</div>
      <div style={{ fontWeight: 800, fontSize: 24, color: "#0f172a", marginTop: 16 }}>
        הבחינה הוגשה בהצלחה!
      </div>
      <div style={{ fontSize: 14, color: "#64748b", marginTop: 8, textAlign: "center", lineHeight: 1.6 }}>
        תשובותיך נשמרו בשרת.<br />ניתן לסגור את הדפדפן.
      </div>
      <div style={{
        marginTop: 24, padding: "12px 24px",
        background: "#dcfce7", borderRadius: 10,
        color: "#15803d", fontSize: 13, fontWeight: 600,
      }}>
        📋 ההגשה הושלמה
      </div>
    </div>
  );
}

// ── Error ─────────────────────────────────────────────────────────────────────
function ErrorScreen({ error, url }: { error: string; url: string }) {
  return (
    <div className="fade-in" style={{ ...s.centerCard, maxWidth: 520 }}>
      <div style={{ fontSize: 48 }}>⚠️</div>
      <div style={{ fontWeight: 800, fontSize: 20, color: "#0f172a", marginTop: 16 }}>
        שגיאת חיבור
      </div>
      <div style={{ fontSize: 13, color: "#64748b", marginTop: 6, marginBottom: 16 }}>
        בדקו שהשרת פתוח ומאזין ב: <code style={{ fontFamily: "monospace", color: "#2563eb" }}>{url}</code>
      </div>
      <pre style={{
        background: "#fef2f2", border: "1.5px solid #fca5a5", borderRadius: 8,
        padding: 14, fontSize: 12, color: "#dc2626",
        whiteSpace: "pre-wrap", wordBreak: "break-all",
        textAlign: "right", width: "100%",
      }}>
        {error}
      </pre>
      <button
        style={{ ...s.btnPrimary, marginTop: 20 }}
        onClick={() => window.location.reload()}
      >
        🔄 נסה שוב
      </button>
    </div>
  );
}

// ── Shared sub-components ─────────────────────────────────────────────────────
function StatBox({ label, value }: { label: string; value: string }) {
  return (
    <div style={{
      flex: 1, padding: "10px 12px", borderRadius: 8,
      border: "1px solid #e2e8f0", background: "#f8fafc",
    }}>
      <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 2 }}>{label}</div>
      <div style={{ fontWeight: 700, fontSize: 14, color: "#0f172a", fontFamily: "ui-monospace, monospace" }}>
        {value}
      </div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────
const s: Record<string, React.CSSProperties> = {
  page: {
    minHeight: "100vh",
    background: "#f1f5f9",
    color: "#0f172a",
    direction: "rtl",
  },
  topBar: {
    position: "sticky", top: 0, zIndex: 50,
    display: "flex", justifyContent: "space-between", alignItems: "center",
    flexWrap: "wrap", gap: 10,
    padding: "12px 20px",
    background: "rgba(255,255,255,0.92)",
    borderBottom: "1px solid #e2e8f0",
    backdropFilter: "blur(12px)",
  },
  logo: {
    width: 38, height: 38, borderRadius: 10,
    background: "#0f172a",
    display: "grid", placeItems: "center",
    fontSize: 20,
  },
  main: {
    maxWidth: 1060,
    margin: "0 auto",
    padding: "24px 16px 48px",
  },
  card: {
    background: "#fff",
    border: "1px solid #e2e8f0",
    borderRadius: 14,
    padding: 18,
    boxShadow: "0 1px 3px rgba(0,0,0,0.06), 0 4px 16px rgba(0,0,0,0.04)",
  },
  centerCard: {
    background: "#fff",
    border: "1px solid #e2e8f0",
    borderRadius: 16,
    padding: 40,
    boxShadow: "0 4px 24px rgba(0,0,0,0.08)",
    display: "flex", flexDirection: "column", alignItems: "center",
    maxWidth: 560, margin: "0 auto",
  },
  badge: {
    display: "flex", alignItems: "center", gap: 6,
    padding: "4px 10px", borderRadius: 99,
    fontSize: 12, fontWeight: 600,
  },
  timerPill: {
    padding: "6px 14px", borderRadius: 99,
    fontWeight: 800, fontSize: 14,
    fontFamily: "ui-monospace, monospace",
  },
  divider: { height: 1, background: "#f1f5f9", margin: "4px 0" },
  examGrid: {
    display: "grid",
    gridTemplateColumns: "1fr 260px",
    gap: 16,
    alignItems: "start",
  },
  qBadge: {
    width: 36, height: 36, borderRadius: 10,
    background: "#2563eb", color: "#fff",
    display: "grid", placeItems: "center",
    fontWeight: 800, fontSize: 16, flexShrink: 0,
  },
  textarea: {
    width: "100%", minHeight: 200, borderRadius: 10,
    border: "1.5px solid #e2e8f0", padding: "12px 14px",
    fontSize: 15, lineHeight: 1.6, resize: "vertical", outline: "none",
    color: "#0f172a",
  },
  btnOutline: {
    padding: "10px 16px", borderRadius: 9,
    border: "1.5px solid #e2e8f0", background: "#fff",
    fontWeight: 600, fontSize: 14, color: "#374151",
    cursor: "pointer", transition: "background 0.15s",
  },
  btnPrimary: {
    padding: "10px 20px", borderRadius: 9,
    border: "none", background: "#2563eb", color: "#fff",
    fontWeight: 700, fontSize: 14,
    cursor: "pointer", transition: "opacity 0.15s",
  },
  btnGhost: {
    padding: "7px 12px", borderRadius: 8,
    border: "1px dashed #cbd5e1", background: "#f8fafc",
    fontSize: 12, color: "#64748b",
    cursor: "pointer", width: "fit-content", marginTop: 4,
  },
  btnSubmit: {
    width: "100%", padding: "14px 0", borderRadius: 10,
    border: "none", background: "#0f172a", color: "#fff",
    fontWeight: 800, fontSize: 15,
    cursor: "pointer", transition: "opacity 0.15s",
  },
  summaryRow: {
    display: "flex", justifyContent: "space-between",
    fontSize: 13, padding: "4px 0",
  },
};

// Responsive: collapse sidebar below exam content on narrow screens
const mq = window.matchMedia?.("(max-width: 780px)");
if (mq?.matches) {
  s.examGrid = { ...s.examGrid, gridTemplateColumns: "1fr" };
}
