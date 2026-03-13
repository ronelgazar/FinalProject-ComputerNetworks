import React, { useEffect, useMemo, useRef, useState } from "react";

type Phase = "boot" | "connecting" | "syncing" | "waiting" | "open" | "running" | "submitted" | "error";

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
  | { type: "schedule_req"; req_id: string; rtt_samples?: number[]; offset_ms?: number }
  | { type: "schedule_resp"; req_id: string; exam_id: string; start_at_server_ms: number; duration_sec: number;
      deliver_delay_ms?: number; max_rtt_ms?: number; my_rtt_ms?: number; relay_candidate?: string | null;
      server_sent_at_ms?: number; bridge_forwarded_at_ms?: number; sync_hold_ms?: number; sync_target_ms?: number }
  | { type: "exam_req"; req_id: string; exam_id: string; rtt_samples?: number[]; offset_ms?: number }
  | { type: "exam_resp"; req_id: string; exam: ExamPayload;
      deliver_delay_ms?: number; max_rtt_ms?: number; my_rtt_ms?: number;
      server_sent_at_ms?: number; bridge_forwarded_at_ms?: number; sync_hold_ms?: number; sync_target_ms?: number }
  | { type: "answers_save"; req_id: string; exam_id: string; answers: Answer[] }
  | { type: "answers_saved"; req_id: string; ok: true }
  | { type: "exam_begin"; req_id: string; exam_id: string; opened_at_ms: number }
  | { type: "exam_begin_ack"; req_id: string; ok: true }
  | { type: "submit_req"; req_id: string; exam_id: string }
  | { type: "submit_resp"; req_id: string; ok: true }
  | { type: "error"; req_id?: string; message: string }
  | { type: "connection_info"; dns_hostname: string; dns_resolver: string;
      resolved_ip: string; all_ips: string[];
      handshake_rtt_ms?: number; rtt_ms?: number };

function rid() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

// ── Adaptive clock-sync helpers ──────────────────────────────────────────────
function syncStdev(samples: SyncSample[], w = 5): number {
  if (samples.length < 2) return Infinity;
  const vals = samples.slice(-w).map(s => s.offset);
  const m = vals.reduce((a, b) => a + b, 0) / vals.length;
  return Math.sqrt(vals.reduce((s, x) => s + (x - m) ** 2, 0) / vals.length);
}
function syncHasConverged(samples: SyncSample[], w = 5, thresh = 1.5): boolean {
  return samples.length >= w && syncStdev(samples, w) < thresh;
}
function buildRttSamples(samples: SyncSample[]): number[] {
  const s = [...samples].sort((a, b) => a.rtt - b.rtt).map(x => x.rtt);
  return [s[0], s[Math.floor(s.length / 2)], s[s.length - 1]];
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

type TransportMode = 'rudp-sync' | 'tcp-sync' | 'tcp-nosync';

const WS_PATHS: Record<TransportMode, string> = {
  'rudp-sync':  '/ws',
  'tcp-sync':   '/ws-tcp-sync',
  'tcp-nosync': '/ws-tcp-nosync',
};

function getInitialMode(): TransportMode {
  const param = new URLSearchParams(window.location.search).get('mode');
  if (param === 'tcp-sync' || param === 'tcp-nosync' || param === 'rudp-sync') return param;
  return 'rudp-sync';
}

function wsUrlForMode(mode: TransportMode): string {
  if (import.meta.env.VITE_WS_URL) return import.meta.env.VITE_WS_URL as string;
  return `ws://${window.location.host}${WS_PATHS[mode]}`;
}

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
  const [mode, setMode] = useState<TransportMode>(getInitialMode());
  const [phase, setPhase] = useState<Phase>("boot");
  const [connectKey, setConnectKey] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WSClient | null>(null);

  const [syncSamples, setSyncSamples] = useState<SyncSample[]>([]);
  const bestSample = useMemo(() => {
    if (syncSamples.length === 0) return null;
    return [...syncSamples].sort((a, b) => a.rtt - b.rtt)[0];
  }, [syncSamples]);
  const offsetMs = bestSample?.offset ?? 0;
  const [syncQuality, setSyncQuality] = useState<"לא ידוע" | "טוב" | "בינוני" | "חלש">("לא ידוע");

  const [connInfo, setConnInfo] = useState<ConnInfo>(null);
  const [syncDelivery, setSyncDelivery] = useState<{
    deliver_delay_ms: number; max_rtt_ms: number; my_rtt_ms: number; relay_candidate: string | null;
  } | null>(null);
  const [syncProof, setSyncProof] = useState<{
    server_sent_at_ms: number; bridge_forwarded_at_ms: number; browser_received_at_ms: number;
    max_rtt_ms: number; my_rtt_ms: number; sync_hold_ms: number; sync_target_ms: number;
  } | null>(null);

  const [examId, setExamId] = useState<string | null>(null);
  const [startAtServerMs, setStartAtServerMs] = useState<number | null>(null);
  const [durationSec, setDurationSec] = useState<number | null>(null);

  const [exam, setExam] = useState<ExamPayload | null>(null);
  const [openedAtMs, setOpenedAtMs] = useState<number | null>(null);   // when exam became available
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

  // ── Boot: connect → sync (adaptive) → schedule → refine → exam ──────────
  // Phases:
  //  1. Quick 3-round bootstrap (50 ms cadence) → schedule_req
  //  2. Adaptive refinement every 200 ms; stop when offset stdev < 1.5 ms
  //     over last 5 samples, T₀ − 8 s reached, or 20 total samples
  //  3. Fresh 3-round sample at T₀ − 5 s (most accurate D_i for server)
  //  4. Sleep to T₀ → exam_req → open
  useEffect(() => {
    if (phase !== "connecting") return;
    let alive = true;
    const allSamples: SyncSample[] = [];

    async function doTimeReq(ws: WSClient): Promise<SyncSample> {
      const req_id = rid();
      const t0 = nowMs();
      const resp = await ws.request<{ type: "time_resp"; req_id: string; server_now_ms: number }>(
        { type: "time_req", req_id, client_t0_ms: t0 }, 2500
      );
      const t1 = nowMs();
      const rtt = t1 - t0;
      return { rtt, offset: resp.server_now_ms - (t0 + rtt / 2), at: t1 };
    }
    function bestSampleNow() {
      return [...allSamples].sort((a, b) => a.rtt - b.rtt)[0];
    }
    function localT0Ms(serverT0: number) {
      return serverT0 - bestSampleNow().offset;
    }
    function updateQuality() {
      const b = bestSampleNow().rtt;
      setSyncQuality(b < 40 ? "טוב" : b < 120 ? "בינוני" : "חלש");
    }

    async function run() {
      try {
        // ─ Connect ────────────────────────────────────────────────────────
        const ws = new WSClient(wsUrlForMode(mode));
        wsRef.current = ws;
        await ws.connect((msg) => {
          if (msg.type === "connection_info") setConnInfo(msg);
        });
        if (!alive) return;

        ws.send({ type: "hello", client_id: `web-${Math.random().toString(36).slice(2, 8)}` });
        setPhase("syncing");

        // ─ Phase 1: 3-round quick bootstrap (50 ms cadence) ──────────────
        for (let i = 0; i < 3; i++) {
          if (!alive) return;
          const s = await doTimeReq(ws);
          allSamples.push(s);
          setSyncSamples([...allSamples]);
          if (i < 2) await new Promise(r => setTimeout(r, 50));
        }
        updateQuality();

        // ─ schedule_req with Phase 1 RTT data ────────────────────────────
        const sched = await ws.request<{
          type: "schedule_resp"; req_id: string; exam_id: string;
          start_at_server_ms: number; duration_sec: number;
          deliver_delay_ms?: number; max_rtt_ms?: number;
          my_rtt_ms?: number; relay_candidate?: string | null;
        }>({
          type: "schedule_req", req_id: rid(),
          rtt_samples: buildRttSamples(allSamples),
          offset_ms: bestSampleNow().offset,
        }, 4000);
        if (!alive) return;

        setExamId(sched.exam_id);
        setStartAtServerMs(sched.start_at_server_ms);
        setDurationSec(sched.duration_sec);
        if (sched.deliver_delay_ms !== undefined) {
          setSyncDelivery({
            deliver_delay_ms: sched.deliver_delay_ms,
            max_rtt_ms: sched.max_rtt_ms ?? 0,
            my_rtt_ms: sched.my_rtt_ms ?? 0,
            relay_candidate: sched.relay_candidate ?? null,
          });
        }
        setPhase("waiting");

        // ─ Phase 2: Adaptive refinement during wait ───────────────────────
        // Collect one sample every 200 ms. Stop when the offset stdev over
        // the last 5 samples is < 1.5 ms (converged), fewer than 8 s remain
        // to T₀, or 20 total samples have been gathered.
        while (alive) {
          const msToT0 = localT0Ms(sched.start_at_server_ms) - nowMs();
          if (msToT0 < 8000) break;
          if (syncHasConverged(allSamples)) break;
          if (allSamples.length >= 20) break;
          await new Promise(r => setTimeout(r, 200));
          if (!alive) return;
          try {
            const s = await doTimeReq(ws);
            allSamples.push(s);
            setSyncSamples([...allSamples]);
            updateQuality();
          } catch { /* network blip — try next round */ }
        }

        // ─ Phase 3: Fresh 3-round sample at T₀ − 5 s ────────────────────
        // These are sent with exam_req so the server uses the most accurate
        // D_i right when the staggered-send coordinator fires.
        const msToRefresh = localT0Ms(sched.start_at_server_ms) - nowMs() - 5000;
        if (msToRefresh > 100 && alive) {
          await new Promise(r => setTimeout(r, msToRefresh));
        }
        if (!alive) return;
        const freshSamples: SyncSample[] = [];
        for (let i = 0; i < 3; i++) {
          if (!alive) return;
          try {
            const s = await doTimeReq(ws);
            freshSamples.push(s);
            allSamples.push(s);
            setSyncSamples([...allSamples]);
          } catch { /* non-critical */ }
          if (i < 2) await new Promise(r => setTimeout(r, 100));
        }

        // ─ Phase 4: Sleep to T₀, then send exam_req ──────────────────────
        const msToExam = localT0Ms(sched.start_at_server_ms) - nowMs();
        if (msToExam > 0 && alive) {
          await new Promise(r => setTimeout(r, clamp(msToExam, 0, 120_000)));
        }
        if (!alive) return;

        const freshForExam = freshSamples.length >= 1 ? {
          rtt_samples: buildRttSamples(freshSamples),
          offset_ms: [...freshSamples].sort((a, b) => a.rtt - b.rtt)[0].offset,
        } : null;

        const openedAt = nowMs();
        const exResp = await ws.request<{
          type: "exam_resp"; req_id: string; exam: ExamPayload;
          server_sent_at_ms?: number; bridge_forwarded_at_ms?: number;
          sync_hold_ms?: number; sync_target_ms?: number;
          max_rtt_ms?: number; my_rtt_ms?: number; deliver_delay_ms?: number;
        }>({
          type: "exam_req", req_id: rid(), exam_id: sched.exam_id,
          ...(freshForExam ? { rtt_samples: freshForExam.rtt_samples, offset_ms: freshForExam.offset_ms } : {}),
        }, 6000);

        const browserReceivedAt = Date.now();
        if (exResp.server_sent_at_ms) {
          setSyncProof({
            server_sent_at_ms:      exResp.server_sent_at_ms,
            bridge_forwarded_at_ms: exResp.bridge_forwarded_at_ms ?? 0,
            browser_received_at_ms: browserReceivedAt,
            max_rtt_ms:    exResp.max_rtt_ms  ?? 0,
            my_rtt_ms:     exResp.my_rtt_ms   ?? 0,
            sync_hold_ms:  exResp.sync_hold_ms ?? exResp.deliver_delay_ms ?? 0,
            sync_target_ms: exResp.sync_target_ms ?? 0,
          });
        }
        setExam(exResp.exam);
        setOpenedAtMs(openedAt);
        const init: Record<string, Answer> = {};
        for (const q of exResp.exam.questions) {
          init[q.id] = q.type === "mcq"
            ? { qid: q.id, type: "mcq", optionId: null }
            : { qid: q.id, type: "text", text: "" };
        }
        setAnswers(init);
        setPhase("open");
      } catch (e: any) {
        setError(e?.message ?? String(e));
        setPhase("error");
      }
    }
    run();
    return () => { alive = false; wsRef.current?.close(); wsRef.current = null; };
  }, [connectKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const syncIsConverged = useMemo(() => syncHasConverged(syncSamples), [syncSamples]);

  // ── open → running: student clicks start ─────────────────────────────────
  const handleStartExam = async () => {
    const ws = wsRef.current;
    if (!ws || !examId || !openedAtMs) return;
    try {
      await ws.request<{ type: "exam_begin_ack"; req_id: string; ok: true }>(
        { type: "exam_begin", req_id: rid(), exam_id: examId, opened_at_ms: openedAtMs }, 4000
      );
    } catch { /* non-critical — transition anyway */ }
    setPhase("running");
  };

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
        {phase === "open" && (
          <div style={{ ...s.timerPill, background: "#f0fdf4", color: "#16a34a", border: "1.5px solid #bbf7d0" }}>
            ✅ הבחינה נפתחה
          </div>
        )}
        {(phase === "running") && msLeft != null && msLeft > 0 && (
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
        {phase === "running" && msLeft != null && msLeft <= 0 && (
          <div style={{ ...s.timerPill, background: "#fee2e2", color: "#dc2626", border: "1.5px solid #fca5a5", animation: "pulse-red 1s ease-in-out infinite" }}>
            ⛔ הזמן נגמר — הגש עכשיו!
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
          {phase === "boot"       && <ModeSelector onSelect={(m) => { setMode(m); setPhase("connecting"); setConnectKey(k => k + 1); }} />}
          {phase === "connecting" && <ConnectingScreen url={wsUrlForMode(mode)} connInfo={connInfo} />}
          {phase === "syncing"    && <SyncPanel samples={syncSamples} total={3} connInfo={connInfo} />}
          {phase === "waiting"    && (
            <WaitingRoom
              examId={examId}
              startAtServerMs={startAtServerMs}
              durationSec={durationSec}
              offsetMs={offsetMs}
              bestSample={bestSample}
              msToStart={msToStart}
              syncDelivery={syncDelivery}
              syncCount={syncSamples.length}
              syncConverged={syncIsConverged}
            />
          )}
          {phase === "open" && exam && startAtServerMs && (
            <ExamOpenedScreen
              exam={exam}
              openedAtMs={openedAtMs!}
              startAtServerMs={startAtServerMs}
              syncProof={syncProof}
              mode={mode}
              onStart={handleStartExam}
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
          {phase === "error"     && <ErrorScreen error={error ?? "שגיאה לא ידועה"} url={wsUrlForMode(mode)} />}
        </main>
      </div>
    </>
  );
}

type ConnInfo = {
  dns_hostname: string; dns_resolver: string; resolved_ip: string; all_ips: string[];
  handshake_rtt_ms?: number; rtt_ms?: number;
} | null;

type SyncDelivery = {
  deliver_delay_ms: number; max_rtt_ms: number; my_rtt_ms: number; relay_candidate: string | null;
} | null;

// ── Mode selector (boot phase) ────────────────────────────────────────────────
function ModeSelector({ onSelect }: { onSelect: (m: TransportMode) => void }) {
  const modes: { id: TransportMode; label: string; sub: string; badge: string; badgeBg: string; badgeColor: string }[] = [
    {
      id: 'rudp-sync',
      label: 'RUDP + Sync',
      sub: 'Custom reliable UDP · staggered send · self-correcting bridge hold',
      badge: 'SYNCHRONIZED',
      badgeBg: '#dcfce7',
      badgeColor: '#15803d',
    },
    {
      id: 'tcp-sync',
      label: 'TCP + Sync',
      sub: 'Plain TCP · same staggered-send coordination as RUDP mode',
      badge: 'SYNCHRONIZED',
      badgeBg: '#dbeafe',
      badgeColor: '#1d4ed8',
    },
    {
      id: 'tcp-nosync',
      label: 'TCP + No Sync',
      sub: 'Plain TCP · immediate send · baseline (no coordination)',
      badge: 'BASELINE',
      badgeBg: '#fef3c7',
      badgeColor: '#92400e',
    },
  ];

  return (
    <div className="fade-in" style={{ ...s.centerCard, maxWidth: 560 }}>
      <div style={{ fontSize: 32, marginBottom: 8 }}>🎓</div>
      <div style={{ fontWeight: 700, fontSize: 22, color: "#0f172a", marginBottom: 6 }}>
        מערכת הבחינה
      </div>
      <div style={{ fontSize: 14, color: "#64748b", marginBottom: 28, textAlign: "center" }}>
        בחר מצב תחבורה להדגמה
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, width: "100%" }}>
        {modes.map((m) => (
          <button
            key={m.id}
            onClick={() => onSelect(m.id)}
            style={{
              width: "100%", padding: "16px 20px",
              background: "#fff", border: "1.5px solid #e2e8f0",
              borderRadius: 12, cursor: "pointer", textAlign: "right",
              display: "flex", alignItems: "center", justifyContent: "space-between",
              transition: "border-color 0.15s, box-shadow 0.15s",
              boxShadow: "0 1px 3px rgba(0,0,0,0.06)",
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLButtonElement).style.borderColor = "#93c5fd";
              (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 2px 8px rgba(37,99,235,0.12)";
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLButtonElement).style.borderColor = "#e2e8f0";
              (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 1px 3px rgba(0,0,0,0.06)";
            }}
          >
            <div>
              <div style={{ fontWeight: 700, fontSize: 15, color: "#0f172a", marginBottom: 4 }}>{m.label}</div>
              <div style={{ fontSize: 12, color: "#64748b" }}>{m.sub}</div>
            </div>
            <span style={{
              padding: "3px 10px", borderRadius: 99, fontSize: 11, fontWeight: 700,
              fontFamily: "monospace", whiteSpace: "nowrap", marginLeft: 12,
              background: m.badgeBg, color: m.badgeColor,
            }}>
              {m.badge}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Connecting ────────────────────────────────────────────────────────────────
function ConnectingScreen({ url, connInfo }: { url: string; connInfo: ConnInfo }) {
  return (
    <div className="fade-in" style={s.centerCard}>
      <div className="spinner" />
      <div style={{ fontWeight: 700, fontSize: 18, color: "#0f172a", marginTop: 20 }}>מתחבר לשרת…</div>
      <div style={{ fontSize: 13, color: "#64748b", marginTop: 6, fontFamily: "monospace" }}>{url}</div>
      {connInfo && <DnsInfoCard connInfo={connInfo} />}
    </div>
  );
}

// ── DNS + RTT info card (shown during connecting + syncing) ───────────────────
function DnsInfoCard({ connInfo }: { connInfo: NonNullable<ConnInfo> }) {
  return (
    <div style={{
      width: "100%", marginTop: 20,
      background: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: 10, padding: "14px 16px",
      textAlign: "right",
    }}>
      <div style={{ fontWeight: 700, fontSize: 12, color: "#166534", marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
        <span>🌐</span> DNS Resolution — {connInfo.dns_hostname}
      </div>
      {/* Resolution chain */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", fontSize: 12, fontFamily: "monospace" }}>
        <span style={{ color: "#64748b" }}>Resolver</span>
        <span style={{ color: "#94a3b8" }}>{connInfo.dns_resolver}</span>
        <span style={{ color: "#94a3b8" }}>→</span>
        <span style={{ fontWeight: 700, color: "#0f172a" }}>{connInfo.dns_hostname}</span>
        <span style={{ color: "#94a3b8" }}>→</span>
        {connInfo.all_ips.map((ip) => (
          <span key={ip} style={{
            padding: "2px 8px", borderRadius: 6,
            background: ip === connInfo.resolved_ip ? "#2563eb" : "#e2e8f0",
            color: ip === connInfo.resolved_ip ? "#fff" : "#64748b",
            fontWeight: ip === connInfo.resolved_ip ? 700 : 400,
          }}>
            {ip}{ip === connInfo.resolved_ip ? " ✓" : ""}
          </span>
        ))}
      </div>
      <div style={{ fontSize: 11, color: "#4ade80", marginTop: 8 }}>
        ✓ נפתרה דרך היררכיית DNS: Resolver → Root → TLD (.lan) → Auth (exam.lan)
      </div>
      {/* RTT measurement */}
      {connInfo.handshake_rtt_ms !== undefined && (
        <div style={{ marginTop: 10, display: "flex", gap: 10, flexWrap: "wrap" }}>
          <span style={{
            padding: "3px 10px", borderRadius: 99, fontSize: 11, fontFamily: "monospace",
            background: "#dcfce7", color: "#166534", fontWeight: 700,
          }}>
            RUDP handshake RTT: {connInfo.handshake_rtt_ms.toFixed(1)} ms
          </span>
          {connInfo.rtt_ms !== undefined && (
            <span style={{
              padding: "3px 10px", borderRadius: 99, fontSize: 11, fontFamily: "monospace",
              background: "#eff6ff", color: "#1d4ed8", fontWeight: 700,
            }}>
              EWMA RTT: {connInfo.rtt_ms.toFixed(1)} ms
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ── Sync proof card (shown after exam_resp received) ─────────────────────────
type SyncProofData = {
  server_sent_at_ms: number; bridge_forwarded_at_ms: number; browser_received_at_ms: number;
  max_rtt_ms: number; my_rtt_ms: number; sync_hold_ms: number; sync_target_ms: number;
} | null;

function SyncProofCard({ sp, mode }: { sp: NonNullable<SyncProofData>; mode?: TransportMode }) {
  if (mode === 'tcp-nosync' && !sp.server_sent_at_ms) {
    return (
      <div style={{ marginTop: 16, background: "#fefce8", border: "1px solid #fde047",
                    borderRadius: 10, padding: "14px 16px", textAlign: "center" }}>
        <div style={{ fontWeight: 700, fontSize: 12, color: "#92400e", marginBottom: 6 }}>
          ⚠ Sync disabled — baseline mode
        </div>
        <div style={{ fontSize: 11, color: "#78350f" }}>
          TCP + No Sync: server sends immediately, no staggered delivery or bridge hold.
          Timing fields absent — this is the uncoordinated baseline for comparison.
        </div>
        <span style={{ display: "inline-block", marginTop: 8, padding: "3px 10px", borderRadius: 99,
                       fontSize: 11, fontWeight: 700, fontFamily: "monospace",
                       background: "#fef3c7", color: "#92400e" }}>
          ⚠ UNCOORDINATED
        </span>
      </div>
    );
  }
  const T0      = sp.server_sent_at_ms;
  const transit = sp.bridge_forwarded_at_ms > 0 ? sp.bridge_forwarded_at_ms - T0 : null;
  const total   = sp.browser_received_at_ms - T0;
  const target  = sp.sync_target_ms > 0 ? sp.sync_target_ms - T0 : sp.max_rtt_ms / 2 + 30;
  const delta   = total - target;
  const synced  = Math.abs(delta) < 30;

  const row = (label: string, val: string, color = "#0f172a") => (
    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "3px 0",
                  borderBottom: "1px solid #f1f5f9" }}>
      <span style={{ color: "#64748b" }}>{label}</span>
      <span style={{ fontFamily: "monospace", fontWeight: 700, color }}>{val}</span>
    </div>
  );

  return (
    <div style={{ marginTop: 16, background: synced ? "#f0fdf4" : "#fefce8",
                  border: `1px solid ${synced ? "#86efac" : "#fde047"}`,
                  borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontWeight: 700, fontSize: 12, color: synced ? "#15803d" : "#92400e",
                    marginBottom: 10, display: "flex", alignItems: "center", gap: 6 }}>
        <span>{synced ? "✓" : "⚠"}</span> Synchronized Delivery Proof — exam_resp
        {mode && (
          <span style={{ marginLeft: "auto", padding: "2px 8px", borderRadius: 99, fontSize: 10,
                         fontFamily: "monospace", fontWeight: 700,
                         background: mode === 'rudp-sync' ? "#dcfce7" : "#dbeafe",
                         color: mode === 'rudp-sync' ? "#15803d" : "#1d4ed8" }}>
            {mode === 'rudp-sync' ? 'RUDP+Sync' : 'TCP+Sync'}
          </span>
        )}
      </div>
      {row("Server sent at",         formatTime(T0))}
      {transit !== null && row("Bridge forwarded at", `+${transit.toFixed(1)} ms`)}
      {row("Browser received at",    `+${total.toFixed(1)} ms`)}
      <div style={{ margin: "8px 0 4px", borderTop: "1px solid #e2e8f0", paddingTop: 8 }} />
      {row("Target delivery",        `+${target.toFixed(1)} ms`,  "#2563eb")}
      {row("Delta from target",      `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} ms`,
           Math.abs(delta) < 10 ? "#15803d" : Math.abs(delta) < 30 ? "#d97706" : "#dc2626")}
      <div style={{ marginTop: 8, fontSize: 11, color: "#64748b", fontFamily: "monospace",
                    background: "#f8fafc", borderRadius: 6, padding: "6px 8px", lineHeight: 1.8 }}>
        target = server_sent + max_rtt/2 + buffer<br/>
        {"      "}= T₀ + {sp.max_rtt_ms.toFixed(1)}/2 + 30 = T₀ + {target.toFixed(1)} ms<br/>
        hold   = target − time.now() = {sp.sync_hold_ms.toFixed(1)} ms
      </div>
      <div style={{ marginTop: 8, fontSize: 11, fontWeight: 700, textAlign: "center",
                    color: synced ? "#15803d" : "#92400e" }}>
        {synced
          ? `✓ SYNCHRONIZED — arrived ${Math.abs(delta).toFixed(1)} ms from target`
          : `⚠ ${Math.abs(delta).toFixed(1)} ms off-target (jitter buffer = 30 ms)`}
      </div>
    </div>
  );
}

// ── Synchronized delivery card ────────────────────────────────────────────────
function SyncDeliveryCard({ sd }: { sd: NonNullable<SyncDelivery> }) {
  const isFastest = sd.deliver_delay_ms > sd.max_rtt_ms / 2;
  const pct = sd.max_rtt_ms > 0 ? Math.round((sd.deliver_delay_ms / (sd.max_rtt_ms + 30)) * 100) : 0;

  return (
    <div style={{
      width: "100%", marginTop: 14,
      background: "#eff6ff", border: "1px solid #bfdbfe", borderRadius: 10, padding: "14px 16px",
      textAlign: "right",
    }}>
      <div style={{ fontWeight: 700, fontSize: 12, color: "#1d4ed8", marginBottom: 10, display: "flex", alignItems: "center", gap: 6 }}>
        <span>⚡</span> Synchronized Delivery (per-packet scheduling)
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 10 }}>
        <div style={{ background: "#fff", borderRadius: 8, padding: "8px 12px", border: "1px solid #e2e8f0" }}>
          <div style={{ fontSize: 10, color: "#94a3b8", marginBottom: 2 }}>My RTT to server</div>
          <div style={{ fontWeight: 800, fontSize: 16, fontFamily: "monospace", color: "#0f172a" }}>{sd.my_rtt_ms.toFixed(1)} ms</div>
        </div>
        <div style={{ background: "#fff", borderRadius: 8, padding: "8px 12px", border: "1px solid #e2e8f0" }}>
          <div style={{ fontSize: 10, color: "#94a3b8", marginBottom: 2 }}>Slowest client RTT</div>
          <div style={{ fontWeight: 800, fontSize: 16, fontFamily: "monospace", color: "#0f172a" }}>{sd.max_rtt_ms.toFixed(1)} ms</div>
        </div>
      </div>
      {/* Delivery delay bar */}
      <div style={{ marginBottom: 6 }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#64748b", marginBottom: 4 }}>
          <span>Bridge holds packet for:</span>
          <span style={{ fontWeight: 700, color: "#1d4ed8", fontFamily: "monospace" }}>
            {sd.deliver_delay_ms} ms
          </span>
        </div>
        <div style={{ background: "#e2e8f0", borderRadius: 99, height: 8 }}>
          <div style={{
            width: `${Math.min(pct, 100)}%`, height: 8, borderRadius: 99,
            background: isFastest ? "#2563eb" : "#10b981",
            transition: "width 0.3s",
          }} />
        </div>
      </div>
      <div style={{ fontSize: 11, color: "#3b82f6", lineHeight: 1.5 }}>
        {sd.deliver_delay_ms > 0
          ? `delay = (max_rtt − my_rtt)/2 + buffer = ${sd.deliver_delay_ms} ms → הגשר מחכה לפני הצגה`
          : `הלקוח האיטי ביותר — delay = buffer בלבד (${sd.deliver_delay_ms} ms)`}
        {sd.relay_candidate && (
          <span style={{ display: "block", marginTop: 4, color: "#7c3aed" }}>
            📡 Relay candidate: {sd.relay_candidate} (lowest RTT — best relay node)
          </span>
        )}
      </div>
    </div>
  );
}

// ── Sync ──────────────────────────────────────────────────────────────────────
function SyncPanel({ samples, total, connInfo }: { samples: SyncSample[]; total: number; connInfo: ConnInfo }) {
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
              <div key={i} title={`RTT: ${s.rtt.toFixed(1)}ms`} style={{
                flex: 1, height: h, borderRadius: 3,
                background: isBest ? "#2563eb" : "#bfdbfe",
              }} />
            );
          })}
        </div>
      )}
      <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>
        עמודה כחולה כהה = דגימה בשימוש (RTT מינימלי)
      </div>
      {connInfo && <DnsInfoCard connInfo={connInfo} />}
    </div>
  );
}

// ── Waiting room ──────────────────────────────────────────────────────────────
function WaitingRoom({
  examId, startAtServerMs, durationSec, offsetMs, bestSample, msToStart, syncDelivery,
  syncCount, syncConverged,
}: {
  examId: string | null; startAtServerMs: number | null; durationSec: number | null;
  offsetMs: number; bestSample: SyncSample | null; msToStart: number | null;
  syncDelivery: SyncDelivery; syncCount: number; syncConverged: boolean;
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
        <div style={{ fontSize: 11, marginTop: 4, color: syncConverged ? "#16a34a" : "#94a3b8" }}>
          {syncConverged
            ? `✓ שעון מסונכרן — ${syncCount} דגימות, מיוצב`
            : `🔄 מדייק שעון... (${syncCount} דגימות)`}
        </div>

        <div style={{
          padding: 12, borderRadius: 8,
          background: "#fff7ed", border: "1px solid #fed7aa",
          fontSize: 12, color: "#92400e",
        }}>
          ⚠ אין לסגור את הדפדפן. גם אם יש עיכוב ברשת — ההתחלה מתוזמנת לשעון המקומי המחושב.
        </div>
      </div>

      {/* Synchronized delivery details */}
      {syncDelivery && <SyncDeliveryCard sd={syncDelivery} />}
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

// ── Exam opened (waiting for student to click start) ─────────────────────────
function ExamOpenedScreen({
  exam, openedAtMs, startAtServerMs, syncProof, mode, onStart,
}: {
  exam: ExamPayload;
  openedAtMs: number;
  startAtServerMs: number;
  syncProof: SyncProofData;
  mode: TransportMode;
  onStart: () => void;
}) {
  const deltaMs = Math.round(openedAtMs - startAtServerMs);
  const sign = deltaMs >= 0 ? "+" : "";

  return (
    <div className="fade-in" style={{ ...s.centerCard, maxWidth: 520 }}>
      <div style={{ fontSize: 56 }}>🔓</div>
      <div style={{ fontWeight: 800, fontSize: 26, color: "#0f172a", marginTop: 16, textAlign: "center" }}>
        הבחינה נפתחה!
      </div>
      <div style={{ fontSize: 14, color: "#64748b", marginTop: 6, textAlign: "center" }}>
        {exam.title}
      </div>

      {/* Timing proof */}
      <div style={{
        width: "100%", marginTop: 24, padding: "14px 18px",
        background: "#f8fafc", border: "1px solid #e2e8f0", borderRadius: 10,
        display: "grid", gap: 8,
      }}>
        <div style={{ fontWeight: 700, fontSize: 12, color: "#64748b", textTransform: "uppercase", letterSpacing: "0.06em" }}>
          טביעת זמן
        </div>
        <div style={s.summaryRow}>
          <span style={{ color: "#64748b" }}>T₀ רשמי (שרת)</span>
          <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{new Date(startAtServerMs).toLocaleTimeString("he-IL", { hour12: false })}.{String(startAtServerMs % 1000).padStart(3, "0")}</span>
        </div>
        <div style={s.summaryRow}>
          <span style={{ color: "#64748b" }}>נפתח אצלי</span>
          <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{new Date(openedAtMs).toLocaleTimeString("he-IL", { hour12: false })}.{String(Math.round(openedAtMs) % 1000).padStart(3, "0")}</span>
        </div>
        <div style={s.summaryRow}>
          <span style={{ color: "#64748b" }}>הפרש מ-T₀</span>
          <span style={{
            fontFamily: "monospace", fontWeight: 700,
            color: Math.abs(deltaMs) < 200 ? "#16a34a" : Math.abs(deltaMs) < 500 ? "#d97706" : "#dc2626",
          }}>
            {sign}{deltaMs} ms
          </span>
        </div>
        <div style={s.summaryRow}>
          <span style={{ color: "#64748b" }}>שאלות</span>
          <span style={{ fontWeight: 700 }}>{exam.questions.length} · {exam.duration_sec / 60} דקות</span>
        </div>
      </div>

      <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 16, textAlign: "center" }}>
        {exam.instructions}
      </div>

      {/* Self-correcting sync proof — shows actual bridge hold and delta from target */}
      {syncProof && <SyncProofCard sp={syncProof} mode={mode} />}
      {!syncProof && mode === 'tcp-nosync' && (
        <div style={{ marginTop: 16, background: "#fefce8", border: "1px solid #fde047",
                      borderRadius: 10, padding: "14px 16px", textAlign: "center" }}>
          <div style={{ fontWeight: 700, fontSize: 12, color: "#92400e", marginBottom: 6 }}>
            ⚠ Sync disabled — baseline mode
          </div>
          <div style={{ fontSize: 11, color: "#78350f" }}>
            TCP + No Sync: server sends immediately, no staggered delivery or bridge hold.
            Use this mode as the uncoordinated baseline to compare against RUDP+Sync / TCP+Sync.
          </div>
          <span style={{ display: "inline-block", marginTop: 8, padding: "3px 10px", borderRadius: 99,
                         fontSize: 11, fontWeight: 700, fontFamily: "monospace",
                         background: "#fef3c7", color: "#92400e" }}>
            ⚠ UNCOORDINATED
          </span>
        </div>
      )}

      <button
        style={{ ...s.btnSubmit, marginTop: 28, width: "100%", fontSize: 17, padding: "16px 0", background: "#2563eb" }}
        onClick={onStart}
      >
        התחל לענות ←
      </button>
      <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 10 }}>
        לחיצה זו תירשם כשעת תחילת המענה שלך
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
