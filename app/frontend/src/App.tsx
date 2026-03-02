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

// ----- Clock helpers -----
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
  if (d < 1000) return "0 שנ׳";
  const s = Math.floor(d / 1000);
  if (s < 60) return `${s} שנ׳`;
  const m = Math.floor(s / 60);
  return `${m} דק׳`;
}

function Mono({ children }: { children: React.ReactNode }) {
  return <span style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace" }}>{children}</span>;
}

// ----- Simple WS RPC (request/response by req_id) -----
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
  // lightweight unique id
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
          try {
            msg = JSON.parse(ev.data);
          } catch {
            return;
          }
          // route response to pending by req_id when exists
          const anyMsg: any = msg as any;
          const reqId = anyMsg.req_id as string | undefined;
          if (reqId && this.pending.has(reqId)) {
            const p = this.pending.get(reqId)!;
            this.pending.delete(reqId);
            if (msg.type === "error") p.reject(new Error(msg.message));
            else p.resolve(msg);
            return;
          }
          // otherwise event
          this.onEvent?.(msg);
        };
        this.ws.onclose = () => {
          // reject all pending
          for (const [k, p] of this.pending) {
            p.reject(new Error("החיבור נסגר"));
            this.pending.delete(k);
          }
        };
      } catch (e) {
        reject(e);
      }
    });
  }

  close() {
    this.ws?.close();
    this.ws = null;
  }

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
        resolve: (v) => {
          window.clearTimeout(t);
          resolve(v as TResp);
        },
        reject: (e) => {
          window.clearTimeout(t);
          reject(e);
        },
      });
    });
  }
}

const WS_URL = import.meta.env.VITE_WS_URL || `ws://${window.location.host}/ws`;

export default function App() {
  const [phase, setPhase] = useState<Phase>("boot");
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WSClient | null>(null);

  // Sync
  const [syncSamples, setSyncSamples] = useState<SyncSample[]>([]);
  const bestSample = useMemo(() => {
    if (syncSamples.length === 0) return null;
    return [...syncSamples].sort((a, b) => a.rtt - b.rtt)[0];
  }, [syncSamples]);
  const offsetMs = bestSample?.offset ?? 0;
  const [syncQuality, setSyncQuality] = useState<"לא ידוע" | "טוב" | "בינוני" | "חלש">("לא ידוע");

  // Schedule
  const [examId, setExamId] = useState<string | null>(null);
  const [startAtServerMs, setStartAtServerMs] = useState<number | null>(null);
  const [durationSec, setDurationSec] = useState<number | null>(null);

  // Exam
  const [exam, setExam] = useState<ExamPayload | null>(null);
  const [activeQ, setActiveQ] = useState<number>(0);
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [saving, setSaving] = useState<boolean>(false);
  const [lastSavedAt, setLastSavedAt] = useState<number | null>(null);
  const autosaveTimer = useRef<number | null>(null);

  // clock ticker
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
    const end = startAtServerMs + durationSec * 1000;
    return end - serverNow;
  }, [phase, startAtServerMs, durationSec, serverNow]);

  // Boot: connect -> sync -> schedule -> waiting
  useEffect(() => {
    let alive = true;

    async function run() {
      try {
        setPhase("connecting");
        const ws = new WSClient(WS_URL);
        wsRef.current = ws;

        await ws.connect();
        if (!alive) return;

        // optional hello
        ws.send({ type: "hello", client_id: `web-${Math.random().toString(36).slice(2, 8)}` });

        setPhase("syncing");

        const samples: SyncSample[] = [];
        const rounds = 12;

        for (let i = 0; i < rounds; i++) {
          const req_id = rid();
          const t0 = nowMs();
          const resp = await ws.request<{ type: "time_resp"; req_id: string; server_now_ms: number }>(
            { type: "time_req", req_id, client_t0_ms: t0 },
            2500
          );
          const t1 = nowMs();
          const rtt = t1 - t0;
          const tMid = t0 + rtt / 2;
          const offset = resp.server_now_ms - tMid;

          samples.push({ rtt, offset, at: t1 });
          setSyncSamples([...samples]);

          await new Promise((r) => setTimeout(r, 120));
        }

        const best = [...samples].sort((a, b) => a.rtt - b.rtt)[0];
        if (best.rtt < 40) setSyncQuality("טוב");
        else if (best.rtt < 120) setSyncQuality("בינוני");
        else setSyncQuality("חלש");

        const sched = await ws.request<{ type: "schedule_resp"; req_id: string; exam_id: string; start_at_server_ms: number; duration_sec: number }>(
          { type: "schedule_req", req_id: rid() },
          4000
        );

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
    return () => {
      alive = false;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  // waiting -> running exactly at computed local start time
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
          { type: "exam_req", req_id: rid(), exam_id: examId },
          6000
        );

        setExam(exResp.exam);

        // init answers
        const init: Record<string, Answer> = {};
        for (const q of exResp.exam.questions) {
          init[q.id] =
            q.type === "mcq"
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

  // autosave every 10 seconds
  useEffect(() => {
    if (phase !== "running" || !examId) return;

    if (autosaveTimer.current) window.clearInterval(autosaveTimer.current);
    autosaveTimer.current = window.setInterval(async () => {
      try {
        const ws = wsRef.current;
        if (!ws) return;
        setSaving(true);
        await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
          { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) },
          4000
        );
        setLastSavedAt(nowMs());
      } catch {
        // soft-fail autosave
      } finally {
        setSaving(false);
      }
    }, 10_000);

    return () => {
      if (autosaveTimer.current) window.clearInterval(autosaveTimer.current);
      autosaveTimer.current = null;
    };
  }, [phase, examId, answers]);

  // auto-submit when time is up
  useEffect(() => {
    if (phase !== "running") return;
    if (msLeft == null) return;
    if (msLeft > 0) return;

    (async () => {
      try {
        const ws = wsRef.current;
        if (!ws || !examId) return;

        setSaving(true);
        await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
          { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) },
          4000
        );
        await ws.request<{ type: "submit_resp"; req_id: string; ok: true }>(
          { type: "submit_req", req_id: rid(), exam_id: examId },
          5000
        );
        setPhase("submitted");
      } catch (e: any) {
        setError(e?.message ?? String(e));
        setPhase("error");
      } finally {
        setSaving(false);
      }
    })();
  }, [phase, msLeft, examId, answers]);

  const header = (
    <div style={styles.topBar}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={styles.logoDot} />
        <div>
          <div style={{ fontWeight: 900, letterSpacing: 0.2 }}>מערכת בחינה</div>
          <div style={{ fontSize: 12, opacity: 0.8 }}>
            זמן שרת: <Mono>{formatTime(serverNow)}</Mono> · סנכרון:{" "}
            <span style={badgeStyle(syncQuality)}>{syncQuality}</span>
          </div>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {phase === "waiting" && msToStart != null && (
          <div style={styles.pill}>
            תחילת בחינה בעוד <Mono>{formatDuration(msToStart)}</Mono>
          </div>
        )}
        {phase === "running" && msLeft != null && (
          <div style={styles.pill}>
            זמן נותר <Mono>{formatDuration(msLeft)}</Mono>
          </div>
        )}
        <div style={{ fontSize: 12, opacity: 0.8 }}>
          {saving ? "שומר…" : lastSavedAt ? `נשמר לפני ${timeAgo(lastSavedAt)}` : "לא נשמר עדיין"}
        </div>
      </div>
    </div>
  );

  return (
    <div style={styles.page}>
      {header}

      <div style={styles.container}>
        {phase === "connecting" && <Card title="מתחבר לשרת…"><div style={styles.small}>מחבר Socket אל <Mono>{WS_URL}</Mono></div></Card>}
        {phase === "syncing" && <SyncPanel samples={syncSamples} />}
        {phase === "waiting" && (
          <WaitingRoom
            examId={examId}
            startAtServerMs={startAtServerMs}
            durationSec={durationSec}
            serverNow={serverNow}
            offsetMs={offsetMs}
            bestSample={bestSample}
          />
        )}
        {phase === "running" && exam && (
          <ExamView
            exam={exam}
            activeQ={activeQ}
            setActiveQ={setActiveQ}
            answers={answers}
            setAnswers={setAnswers}
            onSave={async () => {
              const ws = wsRef.current;
              if (!ws || !examId) return;
              setSaving(true);
              try {
                await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
                  { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) },
                  4000
                );
                setLastSavedAt(nowMs());
              } finally {
                setSaving(false);
              }
            }}
            onSubmit={async () => {
              const ws = wsRef.current;
              if (!ws || !examId) return;
              if (!confirm("להגיש את הבחינה? לאחר ההגשה לא ניתן לערוך.")) return;

              setSaving(true);
              try {
                await ws.request<{ type: "answers_saved"; req_id: string; ok: true }>(
                  { type: "answers_save", req_id: rid(), exam_id: examId, answers: Object.values(answers) },
                  4000
                );
                await ws.request<{ type: "submit_resp"; req_id: string; ok: true }>(
                  { type: "submit_req", req_id: rid(), exam_id: examId },
                  5000
                );
                setPhase("submitted");
              } catch (e: any) {
                setError(e?.message ?? String(e));
                setPhase("error");
              } finally {
                setSaving(false);
              }
            }}
          />
        )}
        {phase === "submitted" && <Submitted />}
        {phase === "error" && <ErrorView error={error ?? "שגיאה לא ידועה"} />}
      </div>

      <footer style={styles.footer}>
        <div style={{ opacity: 0.8 }}>
          טיפ דמו: פתחו כמה טאבים במקביל והוסיפו latency/loss ברמת ה־VM כדי להראות שהתחלה עדיין מסונכרנת.
        </div>
      </footer>
    </div>
  );
}

function SyncPanel({ samples }: { samples: SyncSample[] }) {
  const best = useMemo(() => {
    if (!samples.length) return null;
    return [...samples].sort((a, b) => a.rtt - b.rtt)[0];
  }, [samples]);

  return (
    <Card title="סנכרון שעון מול השרת">
      <div style={{ display: "grid", gap: 10 }}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Stat label="מס׳ דגימות" value={String(samples.length)} />
          <Stat label="RTT הטוב ביותר" value={best ? `${best.rtt.toFixed(1)} ms` : "—"} />
          <Stat label="Offset הטוב ביותר" value={best ? `${best.offset.toFixed(1)} ms` : "—"} />
        </div>

        <div style={styles.small}>
          אנחנו מבצעים מספר בדיקות זמן ושומרים את הדגימה עם RTT הנמוך ביותר כקירוב הטוב ביותר להפרש השעונים.
        </div>

        <div style={styles.table}>
          <div style={styles.tableHead}>
            <div>#</div>
            <div>RTT</div>
            <div>Offset</div>
            <div>זמן</div>
          </div>
          <div>
            {samples.slice(-8).map((s, i) => (
              <div key={i} style={styles.tableRow}>
                <div>{Math.max(1, samples.length - 8 + i + 1)}</div>
                <div>
                  <Mono>{s.rtt.toFixed(1)} ms</Mono>
                </div>
                <div>
                  <Mono>{s.offset.toFixed(1)} ms</Mono>
                </div>
                <div>
                  <Mono>{formatTime(s.at)}</Mono>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Card>
  );
}

function WaitingRoom({
  examId,
  startAtServerMs,
  durationSec,
  serverNow,
  offsetMs,
  bestSample,
}: {
  examId: string | null;
  startAtServerMs: number | null;
  durationSec: number | null;
  serverNow: number;
  offsetMs: number;
  bestSample: SyncSample | null;
}) {
  const startsIn = startAtServerMs ? startAtServerMs - serverNow : null;

  return (
    <Card title="חדר המתנה">
      <div style={{ display: "grid", gap: 14 }}>
        <div style={styles.banner}>
          <div style={{ fontWeight: 900, fontSize: 18 }}>הבחינה תתחיל בו־זמנית לכל הנבחנים</div>
          <div style={styles.small}>המערכת מפצה על הפרשי שעון וזמן רשת כדי להתחיל בדיוק בזמן השרת.</div>
        </div>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Stat label="Exam ID" value={examId ?? "—"} />
          <Stat label="זמן התחלה (שרת)" value={startAtServerMs ? formatTime(startAtServerMs) : "—"} />
          <Stat label="נותר עד התחלה" value={startsIn != null ? formatDuration(startsIn) : "—"} />
          <Stat label="משך" value={durationSec != null ? `${durationSec} שנ׳` : "—"} />
        </div>

        <div style={styles.hr} />

        <div style={{ display: "grid", gap: 8 }}>
          <div style={{ fontWeight: 800 }}>פרטי סנכרון</div>
          <div style={styles.small}>
            RTT מינימלי: <Mono>{bestSample ? `${bestSample.rtt.toFixed(1)} ms` : "—"}</Mono> · Offset:{" "}
            <Mono>{bestSample ? `${bestSample.offset.toFixed(1)} ms` : `${offsetMs.toFixed(1)} ms`}</Mono>
          </div>
          <div style={styles.small}>
            זמן התחלה מקומי מחושב:{" "}
            <Mono>{startAtServerMs ? formatTime(startAtServerMs - offsetMs) : "—"}</Mono>
          </div>
        </div>

        <div style={styles.note}>לא לסגור את הדפדפן. גם אם יש עיכובים — ההתחלה מתוזמנת לזמן המקומי המחושב.</div>
      </div>
    </Card>
  );
}

function ExamView({
  exam,
  activeQ,
  setActiveQ,
  answers,
  setAnswers,
  onSave,
  onSubmit,
}: {
  exam: ExamPayload;
  activeQ: number;
  setActiveQ: (n: number) => void;
  answers: Record<string, Answer>;
  setAnswers: React.Dispatch<React.SetStateAction<Record<string, Answer>>>;
  onSave: () => Promise<void>;
  onSubmit: () => Promise<void>;
}) {
  const q = exam.questions[activeQ];

  return (
    <div style={styles.examGrid}>
      <Card title={exam.title}>
        <div style={{ display: "grid", gap: 10 }}>
          <div style={styles.small}>{exam.instructions}</div>
          <div style={styles.hr} />
          <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
            <div style={{ fontWeight: 900 }}>
              שאלה {activeQ + 1} / {exam.questions.length}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button style={styles.btn} onClick={onSave}>
                שמירה
              </button>
              <button style={{ ...styles.btn, ...styles.btnPrimary }} onClick={onSubmit}>
                הגשה
              </button>
            </div>
          </div>

          <div style={styles.questionBox}>
            <div style={{ fontWeight: 900, marginBottom: 10 }}>{q.prompt}</div>

            {q.type === "mcq" ? (
              <div style={{ display: "grid", gap: 8 }}>
                {q.options.map((opt) => {
                  const cur = answers[q.id] as Answer | undefined;
                  const selected = cur?.type === "mcq" ? cur.optionId === opt.id : false;
                  return (
                    <label key={opt.id} style={{ ...styles.option, outline: selected ? "2px solid rgba(0,0,0,0.15)" : "none" }}>
                      <input
                        type="radio"
                        name={q.id}
                        checked={selected}
                        onChange={() =>
                          setAnswers((prev) => ({
                            ...prev,
                            [q.id]: { qid: q.id, type: "mcq", optionId: opt.id },
                          }))
                        }
                      />
                      <div>{opt.text}</div>
                    </label>
                  );
                })}
                <button
                  style={styles.btnGhost}
                  onClick={() =>
                    setAnswers((prev) => ({
                      ...prev,
                      [q.id]: { qid: q.id, type: "mcq", optionId: null },
                    }))
                  }
                >
                  ניקוי בחירה
                </button>
              </div>
            ) : (
              <textarea
                style={styles.textarea}
                placeholder={q.placeholder ?? "הקלידו תשובה כאן…"}
                value={(answers[q.id] as any)?.text ?? ""}
                onChange={(e) =>
                  setAnswers((prev) => ({
                    ...prev,
                    [q.id]: { qid: q.id, type: "text", text: e.target.value },
                  }))
                }
              />
            )}
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", gap: 10 }}>
            <button style={styles.btn} disabled={activeQ === 0} onClick={() => setActiveQ(activeQ - 1)}>
              ← הקודם
            </button>
            <button
              style={{ ...styles.btn, ...styles.btnPrimary }}
              disabled={activeQ === exam.questions.length - 1}
              onClick={() => setActiveQ(activeQ + 1)}
            >
              הבא →
            </button>
          </div>
        </div>
      </Card>

      <Card title="ניווט שאלות">
        <div style={{ display: "grid", gap: 8 }}>
          {exam.questions.map((qq, idx) => {
            const a = answers[qq.id];
            const answered =
              a?.type === "mcq" ? a.optionId != null : a?.type === "text" ? (a.text ?? "").trim().length > 0 : false;

            return (
              <button
                key={qq.id}
                style={{
                  ...styles.qNav,
                  background: idx === activeQ ? "rgba(0,0,0,0.06)" : "white",
                }}
                onClick={() => setActiveQ(idx)}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 10, justifyContent: "space-between" }}>
                  <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                    <div style={styles.qNum}>{idx + 1}</div>
                    <div style={{ textAlign: "right" }}>
                      <div style={{ fontWeight: 800, fontSize: 13 }}>
                        {qq.type === "mcq" ? "רב־ברירה" : "טקסט"}
                        {qq.points != null ? ` · ${qq.points} נק׳` : ""}
                      </div>
                      <div
                        style={{
                          fontSize: 12,
                          opacity: 0.8,
                          overflow: "hidden",
                          whiteSpace: "nowrap",
                          textOverflow: "ellipsis",
                          maxWidth: 260,
                        }}
                      >
                        {qq.prompt}
                      </div>
                    </div>
                  </div>
                  <div style={{ fontSize: 12, opacity: 0.8 }}>{answered ? "✔" : "—"}</div>
                </div>
              </button>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

function Submitted() {
  return (
    <Card title="הבחינה הוגשה">
      <div style={{ display: "grid", gap: 10 }}>
        <div style={{ fontWeight: 900, fontSize: 18 }}>הבחינה הוגשה בהצלחה.</div>
        <div style={styles.small}>אפשר לסגור את הדפדפן.</div>
      </div>
    </Card>
  );
}

function ErrorView({ error }: { error: string }) {
  return (
    <Card title="שגיאה">
      <div style={{ display: "grid", gap: 10 }}>
        <div style={{ fontWeight: 900, fontSize: 16 }}>משהו השתבש</div>
        <pre style={styles.pre}>{error}</pre>
        <div style={styles.small}>
          בדקו שהשרת פתוח ומאזין ב־<Mono>{WS_URL}</Mono>.
        </div>
      </div>
    </Card>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={styles.card}>
      <div style={styles.cardTitle}>{title}</div>
      <div>{children}</div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div style={styles.stat}>
      <div style={{ fontSize: 12, opacity: 0.75 }}>{label}</div>
      <div style={{ fontWeight: 900 }}>{value}</div>
    </div>
  );
}

function badgeStyle(q: "לא ידוע" | "טוב" | "בינוני" | "חלש") {
  const base: React.CSSProperties = {
    padding: "2px 8px",
    borderRadius: 999,
    fontSize: 12,
    fontWeight: 900,
    background: "rgba(0,0,0,0.06)",
  };
  if (q === "טוב") return { ...base, background: "rgba(0,0,0,0.12)" };
  if (q === "בינוני") return { ...base, background: "rgba(0,0,0,0.09)" };
  if (q === "חלש") return { ...base, background: "rgba(0,0,0,0.05)" };
  return base;
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    minHeight: "100vh",
    background: "linear-gradient(180deg, rgba(0,0,0,0.03), rgba(0,0,0,0.01))",
    color: "rgba(0,0,0,0.9)",
    fontFamily: "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial",
    direction: "rtl",
  },
  topBar: {
    position: "sticky",
    top: 0,
    zIndex: 10,
    display: "flex",
    justifyContent: "space-between",
    gap: 14,
    padding: "14px 16px",
    background: "rgba(255,255,255,0.9)",
    borderBottom: "1px solid rgba(0,0,0,0.08)",
    backdropFilter: "blur(10px)",
  },
  logoDot: {
    width: 14,
    height: 14,
    borderRadius: 6,
    background: "rgba(0,0,0,0.85)",
  },
  container: {
    maxWidth: 1100,
    margin: "0 auto",
    padding: "18px 16px",
  },
  footer: {
    maxWidth: 1100,
    margin: "0 auto",
    padding: "24px 16px 36px",
    fontSize: 12,
  },
  card: {
    background: "white",
    border: "1px solid rgba(0,0,0,0.08)",
    borderRadius: 14,
    padding: 16,
    boxShadow: "0 8px 24px rgba(0,0,0,0.06)",
  },
  cardTitle: {
    fontWeight: 900,
    marginBottom: 12,
    letterSpacing: 0.2,
  },
  banner: {
    padding: 14,
    borderRadius: 12,
    background: "rgba(0,0,0,0.04)",
    border: "1px solid rgba(0,0,0,0.06)",
  },
  stat: {
    padding: "10px 12px",
    borderRadius: 12,
    border: "1px solid rgba(0,0,0,0.08)",
    background: "rgba(0,0,0,0.02)",
    minWidth: 160,
  },
  pill: {
    padding: "8px 12px",
    borderRadius: 999,
    border: "1px solid rgba(0,0,0,0.10)",
    background: "rgba(0,0,0,0.03)",
    fontWeight: 900,
    fontSize: 13,
  },
  small: { fontSize: 12, opacity: 0.85, lineHeight: 1.5 },
  hr: { height: 1, background: "rgba(0,0,0,0.08)", border: 0 },
  note: {
    padding: 12,
    borderRadius: 12,
    border: "1px dashed rgba(0,0,0,0.18)",
    background: "rgba(0,0,0,0.02)",
    fontSize: 12,
    opacity: 0.9,
  },
  table: {
    border: "1px solid rgba(0,0,0,0.08)",
    borderRadius: 12,
    overflow: "hidden",
  },
  tableHead: {
    display: "grid",
    gridTemplateColumns: "40px 120px 160px 1fr",
    gap: 10,
    padding: "10px 12px",
    background: "rgba(0,0,0,0.04)",
    fontSize: 12,
    fontWeight: 900,
  },
  tableRow: {
    display: "grid",
    gridTemplateColumns: "40px 120px 160px 1fr",
    gap: 10,
    padding: "10px 12px",
    borderTop: "1px solid rgba(0,0,0,0.06)",
    fontSize: 12,
    alignItems: "center",
  },
  examGrid: {
    display: "grid",
    gridTemplateColumns: "1fr 360px",
    gap: 16,
  },
  questionBox: {
    padding: 14,
    borderRadius: 12,
    border: "1px solid rgba(0,0,0,0.10)",
    background: "rgba(0,0,0,0.02)",
  },
  option: {
    display: "grid",
    gridTemplateColumns: "18px 1fr",
    gap: 10,
    alignItems: "start",
    padding: 10,
    borderRadius: 12,
    border: "1px solid rgba(0,0,0,0.10)",
    background: "white",
    cursor: "pointer",
  },
  textarea: {
    width: "100%",
    minHeight: 180,
    borderRadius: 12,
    border: "1px solid rgba(0,0,0,0.12)",
    padding: 12,
    fontSize: 14,
    resize: "vertical",
    outline: "none",
  },
  btn: {
    padding: "10px 12px",
    borderRadius: 12,
    border: "1px solid rgba(0,0,0,0.14)",
    background: "white",
    cursor: "pointer",
    fontWeight: 900,
  },
  btnPrimary: {
    background: "rgba(0,0,0,0.88)",
    color: "white",
    border: "1px solid rgba(0,0,0,0.88)",
  },
  btnGhost: {
    padding: "8px 10px",
    borderRadius: 12,
    border: "1px dashed rgba(0,0,0,0.18)",
    background: "rgba(0,0,0,0.02)",
    cursor: "pointer",
    fontWeight: 900,
    fontSize: 12,
    width: "fit-content",
  },
  qNav: {
    width: "100%",
    textAlign: "right",
    padding: 10,
    borderRadius: 12,
    border: "1px solid rgba(0,0,0,0.10)",
    background: "white",
    cursor: "pointer",
  },
  qNum: {
    width: 28,
    height: 28,
    borderRadius: 10,
    display: "grid",
    placeItems: "center",
    background: "rgba(0,0,0,0.06)",
    fontWeight: 900,
    fontSize: 12,
  },
  pre: {
    background: "rgba(0,0,0,0.04)",
    border: "1px solid rgba(0,0,0,0.08)",
    borderRadius: 12,
    padding: 12,
    overflow: "auto",
    fontSize: 12,
  },
};

// responsive tweak
const media = window.matchMedia?.("(max-width: 980px)");
if (media?.matches) {
  styles.examGrid = { ...styles.examGrid, gridTemplateColumns: "1fr" };
}