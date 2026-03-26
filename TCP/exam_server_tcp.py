"""
Exam backend over plain TCP.
Framing: newline-delimited JSON (readline on recv, json+\n on send).

Same JSON protocol as RUDP/exam_server.py.

SYNC_ENABLED=1 (default): uses ExamSendCoordinator + staggered sends
                           + server_sent_at_ms stamp.
SYNC_ENABLED=0:            sends immediately, no coordinator,
                           no server_sent_at_ms (bridge/frontend see baseline).
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import pathlib
import time
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [tcp-exam] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

TCP_PORT         = int(os.environ.get('TCP_PORT', '9001'))
START_OFFSET_SEC = int(os.environ.get('START_OFFSET_SEC', '120'))
SHARED_DIR       = pathlib.Path(os.environ.get('SHARED_DIR', '/app/shared'))
ANSWERS_DIR      = pathlib.Path(os.environ.get('ANSWERS_DIR', '/app/answers'))
EXAM_JSON_PATH   = pathlib.Path(os.environ.get('EXAM_JSON', '/app/exam.json'))
EXAM_DURATION_SEC= int(os.environ.get('EXAM_DURATION_SEC', '3600'))
SYNC_ENABLED     = os.environ.get('SYNC_ENABLED', '1') == '1'

# Pre-pinned start time (optional)
_EXAM_START_AT_MS: Optional[int] = (
    int(os.environ['EXAM_START_AT_MS']) if 'EXAM_START_AT_MS' in os.environ else None
)

# ── Per-packet synchronized delivery (only when SYNC_ENABLED=1) ──────────────

_client_rtts: Dict[str, float] = {}
_client_rtt_samples: Dict[str, list] = {}
_client_offsets: Dict[str, float] = {}
_rtt_lock = asyncio.Lock()

C_SEND_MS             = 1.0
M_MARGIN_MS           = 15.0
JITTER_BUFFER_MS      = 5
COLLECTION_WINDOW_SEC = 1.5


async def _register_client_rtt(client_id: str, rtt_ms: float,
                                rtt_samples: Optional[list] = None,
                                offset_ms: Optional[float] = None):
    async with _rtt_lock:
        _client_rtts[client_id] = rtt_ms
        if rtt_samples and len(rtt_samples) >= 3:
            # Store all N samples so _compute_D can use p10/p90 statistics
            _client_rtt_samples[client_id] = sorted(float(x) for x in rtt_samples)
        if offset_ms is not None:
            _client_offsets[client_id] = offset_ms
    log.info("RTT registry: client=%s rtt=%.1fms n_samples=%d offset=%s",
             client_id, rtt_ms, len(rtt_samples) if rtt_samples else 0,
             f"{offset_ms:.2f}ms" if offset_ms is not None else "n/a")


def _get_max_rtt_ms() -> float:
    return max(_client_rtts.values(), default=100.0)


def _compute_D(client_id: Optional[str]) -> float:
    """
    Accurate one-way delay estimate using p10/p90 statistics over all N samples.
    Mirrors the improved formula in RUDP/exam_server.py — see that file for details.
    """
    if not client_id:
        return 50.0
    if client_id in _client_rtt_samples:
        s = _client_rtt_samples[client_id]   # already sorted, all N samples
        n = len(s)
        p10_idx = max(0, int(n * 0.10))
        p90_idx = min(n - 1, int(n * 0.90))
        rtt_p10 = s[p10_idx]
        idr     = s[p90_idx] - rtt_p10
        offset  = _client_offsets.get(client_id)
        if offset is not None:
            d_down = rtt_p10 / 2.0 - offset
            D = max(1.0, d_down + idr * 0.25)
            return D
        return (rtt_p10 + idr) / 2.0
    rtt = _client_rtts.get(client_id, 100.0)
    return rtt / 2.0


def _deliver_delay_ms(client_id: Optional[str]) -> int:
    if not client_id or client_id not in _client_rtts:
        return 0
    max_rtt = _get_max_rtt_ms()
    my_rtt  = _client_rtts[client_id]
    return int(max(0, (max_rtt - my_rtt) / 2 + JITTER_BUFFER_MS))


def _relay_candidate() -> Optional[str]:
    if len(_client_rtts) < 2:
        return None
    return min(_client_rtts, key=lambda k: _client_rtts[k])


# ── ExamSendCoordinator ───────────────────────────────────────────────────────

class ExamSendCoordinator:
    def __init__(self):
        self._registry: Dict[str, float] = {}
        self._t_sends:  Dict[str, float] = {}
        self._event   = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._lock    = asyncio.Lock()

    def reset(self):
        if self._task is not None and not self._task.done():
            return
        self._registry.clear()
        self._t_sends.clear()
        self._event.clear()
        self._task = None
        # Clear cached start time so re-injected start_at.txt is picked up.
        global _EXAM_START_AT_MS
        _EXAM_START_AT_MS = None

    async def register(self, client_id: str, D_i: float):
        async with self._lock:
            self._registry[client_id] = D_i
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._coordinate())

    async def wait_t_send(self, client_id: str) -> float:
        await self._event.wait()
        return self._t_sends.get(client_id, time.time() * 1000)

    async def _coordinate(self):
        await asyncio.sleep(COLLECTION_WINDOW_SEC)
        async with self._lock:
            items = list(self._registry.items())
        if not items:
            self._event.set()
            return
        now_ms      = time.time() * 1000
        max_D       = max(d for _, d in items)
        N           = len(items)
        # Anchor T_arr to the shared start_at_ms for cross-instance consistency.
        start_at_ms = await _get_start_at_ms()
        T_arr       = float(start_at_ms)
        if T_arr < now_ms + max_D + M_MARGIN_MS:
            T_arr = now_ms + max_D + N * C_SEND_MS + M_MARGIN_MS
            log.warning("T_arr: start_at_ms too close/past — dynamic fallback T_arr=+%.0fms",
                        T_arr - now_ms)
        for cid, D in items:
            self._t_sends[cid] = T_arr - D
        sorted_clients = sorted(items, key=lambda x: self._t_sends[x[0]])
        log.info("Coordinator: N=%d max_D=%.1f T_arr=+%.0fms", N, max_D, T_arr - now_ms)
        for i, (cid, D) in enumerate(sorted_clients):
            t_send = self._t_sends[cid]
            if i < len(sorted_clients) - 1:
                next_t = self._t_sends[sorted_clients[i + 1][0]]
                delta  = max(0.0, next_t - t_send - C_SEND_MS)
            else:
                delta = 0.0
            log.info("  [%d] %s D=%.1fms T_send=+%.1fms Δ=%.1fms",
                     i + 1, cid, D, t_send - now_ms, delta)
        self._event.set()


_coordinator = ExamSendCoordinator()

# ── Exam content ──────────────────────────────────────────────────────────────

_DEFAULT_EXAM = {
    "exam_id": "exam-2024-01",
    "title": "בחינת מבוא לרשתות מחשבים",
    "instructions": "ענו על כל השאלות. זמן הבחינה מוצג בפינה הימנית העליונה.",
    "duration_sec": EXAM_DURATION_SEC,
    "questions": [
        {
            "id": "q1",
            "type": "mcq",
            "prompt": "איזה פרוטוקול פועל בשכבת התחבורה של מודל OSI?",
            "options": [
                {"id": "a", "text": "IP"},
                {"id": "b", "text": "TCP"},
                {"id": "c", "text": "HTTP"},
                {"id": "d", "text": "Ethernet"},
            ],
            "points": 25,
        },
        {
            "id": "q2",
            "type": "mcq",
            "prompt": "מה גודל הכתובת IPv4?",
            "options": [
                {"id": "a", "text": "16 bit"},
                {"id": "b", "text": "32 bit"},
                {"id": "c", "text": "64 bit"},
                {"id": "d", "text": "128 bit"},
            ],
            "points": 25,
        },
        {
            "id": "q3",
            "type": "mcq",
            "prompt": "איזה מנגנון משמש ב-TCP להבטחת אמינות?",
            "options": [
                {"id": "a", "text": "ACK + Retransmit"},
                {"id": "b", "text": "Checksum בלבד"},
                {"id": "c", "text": "CRC-32"},
                {"id": "d", "text": "אין מנגנון כזה"},
            ],
            "points": 25,
        },
        {
            "id": "q4",
            "type": "text",
            "prompt": "הסבירו את ההבדל בין UDP ל-TCP בשני משפטים.",
            "placeholder": "הקלידו את תשובתכם כאן…",
            "points": 15,
        },
        {
            "id": "q5",
            "type": "text",
            "prompt": "מה תפקידו של ה-DNS ברשת האינטרנט?",
            "placeholder": "הקלידו את תשובתכם כאן…",
            "points": 10,
        },
    ],
}


def _load_exam() -> Dict[str, Any]:
    for path in [SHARED_DIR / 'exam.json', EXAM_JSON_PATH]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                log.info("Loaded exam from %s", path)
                return data
            except Exception as e:
                log.warning("Failed to load %s: %s", path, e)
    return _DEFAULT_EXAM


# ── Shared start-time ─────────────────────────────────────────────────────────

_start_at_lock = asyncio.Lock()


async def _get_start_at_ms() -> int:
    global _EXAM_START_AT_MS
    if _EXAM_START_AT_MS is not None:
        return _EXAM_START_AT_MS

    async with _start_at_lock:
        if _EXAM_START_AT_MS is not None:
            return _EXAM_START_AT_MS

        start_file = SHARED_DIR / 'start_at.txt'
        SHARED_DIR.mkdir(parents=True, exist_ok=True)

        if start_file.exists():
            try:
                _EXAM_START_AT_MS = int(start_file.read_text().strip())
                log.info("Loaded start_at_ms=%d from %s", _EXAM_START_AT_MS, start_file)
                return _EXAM_START_AT_MS
            except Exception as e:
                log.warning("Could not read %s: %s", start_file, e)

        _EXAM_START_AT_MS = int((time.time() + START_OFFSET_SEC) * 1000)
        start_file.write_text(str(_EXAM_START_AT_MS))
        log.info("Created start_at_ms=%d, saved to %s", _EXAM_START_AT_MS, start_file)
        return _EXAM_START_AT_MS


# ── Client tracking ───────────────────────────────────────────────────────────

CLIENTS_FILE  = SHARED_DIR / 'clients.json'
_clients_lock = asyncio.Lock()


async def _update_client(client_id: str, **fields):
    async with _clients_lock:
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(CLIENTS_FILE.read_text()) if CLIENTS_FILE.exists() else {}
        except Exception:
            data = {}
        if client_id not in data:
            data[client_id] = {}
        data[client_id].update(fields)
        CLIENTS_FILE.write_text(json.dumps(data, indent=2))


def _count_answered(answers) -> int:
    # Flat dict format: {"q1": "a", "q2": "b", ...}
    if isinstance(answers, dict):
        return sum(1 for v in answers.values() if v)
    # Legacy list format: [{"type": "mcq", "optionId": "a"}, ...]
    count = 0
    for a in answers:
        if not isinstance(a, dict):
            continue
        if a.get('type') == 'mcq' and a.get('optionId') is not None:
            count += 1
        elif a.get('type') == 'text' and str(a.get('text', '')).strip():
            count += 1
    return count


def _save_answers(exam_id: str, client_id: str,
                  answers: list, final: bool = False):
    try:
        d = ANSWERS_DIR / exam_id
        d.mkdir(parents=True, exist_ok=True)
        suffix = '.final.json' if final else '.json'
        path = d / f"{client_id}{suffix}"
        path.write_text(json.dumps(answers, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("Failed to save answers: %s", e)


# ── Per-connection handler ────────────────────────────────────────────────────

async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info('peername')
    log.info("New TCP connection from %s", peer)
    client_id: Optional[str] = None
    exam_id: Optional[str]   = None

    try:
        while not reader.at_eof():
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=300)
            except asyncio.TimeoutError:
                log.info("Connection timeout: %s", peer)
                break
            if not line:
                break

            try:
                msg = json.loads(line.decode('utf-8').strip())
            except json.JSONDecodeError as e:
                log.warning("JSON error from %s: %s", peer, e)
                await _send(writer, {"type": "error", "message": "invalid JSON"})
                continue

            msg_type = msg.get('type', '')
            req_id   = msg.get('req_id')

            if msg_type == 'hello':
                client_id = msg.get('client_id', 'unknown')
                rtt_ms    = float(msg.get('rtt_ms', 50.0))
                log.info("hello from client_id=%s rtt_ms=%.1f", client_id, rtt_ms)
                if SYNC_ENABLED:
                    await _register_client_rtt(client_id, rtt_ms)
                await _update_client(client_id,
                    connected_at=int(time.time() * 1000),
                    rtt_ms=round(rtt_ms, 2),
                    transport='tcp-sync' if SYNC_ENABLED else 'tcp-nosync',
                    submitted=False,
                    answers_count=0,
                )
                await _send(writer, {"type": "hello_ack", "client_id": client_id})

            elif msg_type == 'time_req':
                await _send(writer, {
                    "type": "time_resp",
                    "req_id": req_id,
                    "server_now_ms": int(time.time() * 1000),
                })

            elif msg_type == 'schedule_req':
                if SYNC_ENABLED:
                    rtt_samples = msg.get('rtt_samples')
                    if rtt_samples and len(rtt_samples) >= 3 and client_id:
                        s_sorted = sorted(rtt_samples)
                        rtt_median = s_sorted[len(s_sorted) // 2]
                        await _register_client_rtt(client_id,
                                                   rtt_median,
                                                   s_sorted,
                                                   offset_ms=msg.get('offset_ms'))
                    _coordinator.reset()

                start_ms     = await _get_start_at_ms()
                current_exam = _load_exam()
                exam_id      = current_exam['exam_id']

                resp: Dict[str, Any] = {
                    "type":               "schedule_resp",
                    "req_id":             req_id,
                    "exam_id":            exam_id,
                    "start_at_server_ms": start_ms,
                    "duration_sec":       current_exam['duration_sec'],
                }
                if SYNC_ENABLED:
                    D     = _compute_D(client_id)
                    delay = _deliver_delay_ms(client_id)
                    resp.update({
                        "deliver_delay_ms": delay,
                        "max_rtt_ms":       round(_get_max_rtt_ms(), 2),
                        "my_rtt_ms":        round(_client_rtts.get(client_id, 50.0) if client_id else 50.0, 2),
                        "D_i":              round(D, 2),
                        "relay_candidate":  _relay_candidate(),
                    })
                await _send(writer, resp)

            elif msg_type == 'exam_req':
                if SYNC_ENABLED:
                    fresh_samples = msg.get('rtt_samples')
                    fresh_offset  = msg.get('offset_ms')
                    if fresh_samples and len(fresh_samples) >= 3 and client_id:
                        fs_sorted = sorted(fresh_samples)
                        fs_median = fs_sorted[len(fs_sorted) // 2]
                        await _register_client_rtt(client_id, fs_median,
                                                   fs_sorted, offset_ms=fresh_offset)

                # ── Pre-encode exam BEFORE stagger wait ───────────────────────
                # json.dumps on the 33 KB exam dict takes ~27 ms.  If this
                # happens AFTER the stagger sleep, each client's serialization
                # adds variable latency to server_sent_at_ms, defeating the
                # coordinator's timing.  Pre-encoding here (during the
                # COLLECTION_WINDOW_SEC) moves that cost to before T_send,
                # leaving only a tiny meta-dict to serialize on the hot path.
                current_exam  = _load_exam()
                exam_json_str = json.dumps(current_exam)   # ~27 ms, done now

                if SYNC_ENABLED:
                    D = _compute_D(client_id)
                    await _coordinator.register(client_id or 'unknown', D)
                    t_send = await _coordinator.wait_t_send(client_id or 'unknown')

                    wait_ms = t_send - time.time() * 1000
                    if wait_ms > 0:
                        log.info("Stagger send: client=%s D=%.1fms waiting=%.1fms",
                                 client_id, D, wait_ms)
                        await asyncio.sleep(wait_ms / 1000.0)

                # ── Hot path: only tiny meta dict serialized after stagger ───
                # server_sent_at_ms stamped here; exam_json_str injected via
                # string concatenation (no re-serialization of the 33 KB exam).
                if SYNC_ENABLED:
                    delay = _deliver_delay_ms(client_id)
                    meta = {
                        "type":              "exam_resp",
                        "req_id":            req_id,
                        "server_sent_at_ms": int(time.time() * 1000),
                        "deliver_delay_ms":  delay,
                        "max_rtt_ms":        round(_get_max_rtt_ms(), 2),
                        "my_rtt_ms":         round(_client_rtts.get(client_id, 50.0) if client_id else 50.0, 2),
                        "D_i":               round(D, 2),
                        "T_send_planned_ms": round(t_send, 2),
                        "stagger_wait_ms":   round(max(0.0, wait_ms), 2),
                    }
                    # Inject pre-serialized exam without re-running json.dumps
                    meta_str = json.dumps(meta)
                    line = meta_str[:-1] + ',"exam":' + exam_json_str + '}\n'
                else:
                    line = json.dumps({"type": "exam_resp", "req_id": req_id,
                                       "exam": current_exam}) + '\n'
                try:
                    writer.write(line.encode('utf-8'))
                    await writer.drain()
                except Exception as e:
                    log.warning("exam_resp write failed: %s", e)

                if client_id:
                    await _update_client(client_id,
                        exam_opened_at=int(time.time() * 1000),
                        total_questions=len(current_exam.get('questions', [])),
                    )

            elif msg_type == 'exam_begin':
                opened_at_ms = msg.get('opened_at_ms', int(time.time() * 1000))
                await _send(writer, {"type": "exam_begin_ack", "req_id": req_id, "ok": True})
                if client_id:
                    await _update_client(client_id,
                        exam_opened_at=opened_at_ms,
                        student_started_at=int(time.time() * 1000),
                    )

            elif msg_type == 'answers_save':
                answers = msg.get('answers', [])
                eid = msg.get('exam_id', exam_id or 'unknown')
                _save_answers(eid, client_id or 'unknown', answers)
                await _send(writer, {"type": "answers_saved", "req_id": req_id, "ok": True})
                if client_id:
                    await _update_client(client_id,
                        answers_count=_count_answered(answers),
                        last_active=int(time.time() * 1000),
                    )

            elif msg_type == 'submit_req':
                answers = msg.get('answers', [])
                eid = msg.get('exam_id', exam_id or 'unknown')
                _save_answers(eid, client_id or 'unknown', answers, final=True)
                log.info("Submission from client_id=%s", client_id)
                await _send(writer, {"type": "submit_resp", "req_id": req_id, "ok": True})
                if client_id:
                    await _update_client(client_id,
                        submitted=True,
                        submitted_at=int(time.time() * 1000),
                        answers_count=_count_answered(answers),
                    )

            else:
                await _send(writer, {
                    "type":    "error",
                    "req_id":  req_id,
                    "message": f"unknown message type: {msg_type}",
                })

    except Exception as e:
        log.exception("Connection error from %s: %s", peer, e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        log.info("Connection closed: peer=%s client_id=%s", peer, client_id)


async def _send(writer: asyncio.StreamWriter, obj: dict):
    if SYNC_ENABLED:
        obj['server_sent_at_ms'] = int(time.time() * 1000)
    data = json.dumps(obj).encode('utf-8') + b'\n'
    try:
        writer.write(data)
        await writer.drain()
    except Exception as e:
        log.warning("TCP send failed: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    server = await asyncio.start_server(handle_connection, host, TCP_PORT)
    mode = "SYNC" if SYNC_ENABLED else "NO-SYNC (baseline)"
    log.info("TCP exam server listening on %s:%d  [%s]", host, TCP_PORT, mode)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
