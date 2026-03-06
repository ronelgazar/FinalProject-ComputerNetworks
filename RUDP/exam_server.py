"""
Exam backend over RUDP.
Handles the JSON protocol that App.tsx speaks:
  hello, time_req/resp, schedule_req/resp, exam_req/resp,
  answers_save/saved, submit_req/resp.

EXAM_START_AT_MS env var pins the start time.
If unset, computed on first schedule_req and written to /app/shared/start_at.txt
so all scaled instances share the same timestamp.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import pathlib
import time
from typing import Any, Dict, Optional

from rudp_socket import RudpServer, RudpSocket, RudpTimeout, RudpReset

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [exam] %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

RUDP_PORT        = int(os.environ.get('RUDP_PORT', '9000'))
START_OFFSET_SEC = int(os.environ.get('START_OFFSET_SEC', '120'))
SHARED_DIR       = pathlib.Path(os.environ.get('SHARED_DIR', '/app/shared'))
ANSWERS_DIR      = pathlib.Path(os.environ.get('ANSWERS_DIR', '/app/answers'))
EXAM_JSON_PATH   = pathlib.Path(os.environ.get('EXAM_JSON', '/app/exam.json'))
EXAM_DURATION_SEC= int(os.environ.get('EXAM_DURATION_SEC', '3600'))

# Pre-pinned start time (optional)
_EXAM_START_AT_MS: Optional[int] = (
    int(os.environ['EXAM_START_AT_MS']) if 'EXAM_START_AT_MS' in os.environ else None
)

# ── Per-packet synchronized delivery ─────────────────────────────────────────
#
# Two complementary mechanisms work together:
#
# A) Server-side staggered sends (primary — friend's formula):
#    For each client i, compute a conservative one-way delay estimate D_i:
#      RTT_med_i = median of 3 RTT samples (min/med/max from 12 NTP rounds)
#      J_rtt_i   = max - min  (jitter estimate from 3 samples)
#      D_i       = (RTT_med_i + J_rtt_i) / 2   (conservative one-way delay)
#    Choose target arrival time:
#      T_arr = now + max(D_i) + N*C + M
#        N = number of clients, C = per-send overhead, M = scheduling margin
#    Each client gets send time:
#      T_send[i] = T_arr - D_i
#    Sort clients by T_send ascending (= highest D first) and send in that order,
#    sleeping Δ_k = max(0, T_send[k+1] - T_send[k] - C) between consecutive sends.
#
# B) Bridge-side fine-tuning (secondary — self-correcting):
#    The bridge uses server_sent_at_ms to compute remaining hold dynamically,
#    absorbing any RUDP retransmission delay that already occurred.

_client_rtts: Dict[str, float] = {}             # client_id → RTT in ms (from hello)
_client_rtt_samples: Dict[str, list] = {}        # client_id → [min, med, max] from NTP
_client_offsets: Dict[str, float] = {}           # client_id → NTP clock offset (ms)
_rtt_lock = asyncio.Lock()
JITTER_BUFFER_MS = 30

# Send overhead estimate per client (ms) and scheduling margin (ms)
C_SEND_MS        = 1.0
M_MARGIN_MS      = 15.0
COLLECTION_WINDOW_SEC = 1.5   # how long to wait for all exam_req before scheduling


async def _register_client_rtt(client_id: str, rtt_ms: float,
                                rtt_samples: Optional[list] = None,
                                offset_ms: Optional[float] = None):
    async with _rtt_lock:
        _client_rtts[client_id] = rtt_ms
        if rtt_samples and len(rtt_samples) >= 3:
            _client_rtt_samples[client_id] = sorted(rtt_samples[:3])
        if offset_ms is not None:
            _client_offsets[client_id] = offset_ms
    log.info("RTT registry: client=%s rtt=%.1fms samples=%s offset=%.2fms",
             client_id, rtt_ms, rtt_samples, offset_ms if offset_ms is not None else 0.0)


def _get_max_rtt_ms() -> float:
    return max(_client_rtts.values(), default=100.0)


def _compute_D(client_id: Optional[str]) -> float:
    """
    Conservative one-way delay estimate for a client.

    When fresh NTP offset data is available (from the pre-exam refresh):
      d_down = RTT_med / 2 - offset_ms
      J_rtt  = max(r1,r2,r3) - min(...)
      D_i    = d_down + J_rtt / 2   (asymmetric-corrected estimate)

    Without offset data (friend's formula):
      RTT_med = median(r1, r2, r3)
      J_rtt   = max - min
      D_i     = (RTT_med + J_rtt) / 2

    Falls back to rtt_ms/2 if no 3-sample data is available.
    """
    if not client_id:
        return 50.0
    if client_id in _client_rtt_samples:
        s = sorted(_client_rtt_samples[client_id])
        rtt_med = s[len(s) // 2]
        j_rtt   = s[-1] - s[0]
        offset  = _client_offsets.get(client_id)
        if offset is not None:
            # Asymmetric correction: d_down = RTT/2 - offset
            d_down = rtt_med / 2.0 - offset
            D = max(1.0, d_down + j_rtt / 2.0)
            log.debug("D_i for %s: rtt_med=%.1f offset=%.2f d_down=%.2f j=%.1f → D=%.1f",
                      client_id, rtt_med, offset, d_down, j_rtt, D)
            return D
        return (rtt_med + j_rtt) / 2.0
    rtt = _client_rtts.get(client_id, 100.0)
    return rtt / 2.0   # fallback


def _deliver_delay_ms(client_id: Optional[str]) -> int:
    """Bridge-side hold for bridge self-correcting fallback (secondary mechanism)."""
    if not client_id or client_id not in _client_rtts:
        return 0
    max_rtt = _get_max_rtt_ms()
    my_rtt  = _client_rtts[client_id]
    delay   = max(0, (max_rtt - my_rtt) / 2 + JITTER_BUFFER_MS)
    return int(delay)


def _relay_candidate() -> Optional[str]:
    if len(_client_rtts) < 2:
        return None
    return min(_client_rtts, key=lambda k: _client_rtts[k])


# ── Exam send coordinator (staggered sends) ───────────────────────────────────

class ExamSendCoordinator:
    """
    Collects D_i values from all arriving exam_req handlers,
    then computes a staggered send schedule so all clients receive
    the exam at the same wall-clock moment T_arr.
    """

    def __init__(self):
        self._registry: Dict[str, float] = {}   # {client_id: D_i}
        self._t_sends:  Dict[str, float] = {}   # {client_id: T_send_ms}
        self._event   = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._lock    = asyncio.Lock()

    def reset(self):
        """Reset for a new exam session (called on schedule_req)."""
        self._registry.clear()
        self._t_sends.clear()
        self._event.clear()
        self._task = None

    async def register(self, client_id: str, D_i: float):
        """Register client's D_i and ensure coordinator task is running."""
        async with self._lock:
            self._registry[client_id] = D_i
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._coordinate())

    async def wait_t_send(self, client_id: str) -> float:
        """Block until the coordinator has set T_send for this client."""
        await self._event.wait()
        return self._t_sends.get(client_id, time.time() * 1000)

    async def _coordinate(self):
        """
        Wait COLLECTION_WINDOW_SEC for all exam_req to arrive, then compute:

          T_arr     = now + max(D_i) + N*C + M
          T_send[i] = T_arr - D_i     (higher D → earlier send)

        Stagger Δ_k = max(0, T_send[k+1] - T_send[k] - C) between sends.
        """
        await asyncio.sleep(COLLECTION_WINDOW_SEC)

        async with self._lock:
            items = list(self._registry.items())   # [(client_id, D_i)]

        if not items:
            self._event.set()
            return

        now_ms  = time.time() * 1000
        max_D   = max(d for _, d in items)
        N       = len(items)
        T_arr   = now_ms + max_D + N * C_SEND_MS + M_MARGIN_MS

        # T_send[i] = T_arr - D_i  (slowest D first → smallest T_send → sent first)
        for cid, D in items:
            self._t_sends[cid] = T_arr - D

        sorted_clients = sorted(items, key=lambda x: self._t_sends[x[0]])

        log.info("ExamCoordinator: N=%d max_D=%.1f T_arr=+%.0f ms from now",
                 N, max_D, T_arr - now_ms)
        for i, (cid, D) in enumerate(sorted_clients):
            t_send = self._t_sends[cid]
            if i < len(sorted_clients) - 1:
                next_t = self._t_sends[sorted_clients[i + 1][0]]
                delta  = max(0.0, next_t - t_send - C_SEND_MS)
            else:
                delta = 0.0
            log.info("  [%d] %s D=%.1fms T_send=+%.1fms Δ_next=%.1fms",
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
    # Admin-uploaded exam takes priority over baked-in file
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


def _count_answered(answers: list) -> int:
    count = 0
    for a in answers:
        if a.get('type') == 'mcq' and a.get('optionId') is not None:
            count += 1
        elif a.get('type') == 'text' and str(a.get('text', '')).strip():
            count += 1
    return count


# ── Per-connection handler ────────────────────────────────────────────────────

async def handle_connection(conn: RudpSocket):
    client_id: Optional[str] = None
    exam_id: Optional[str] = None

    log.info("New connection from %s", conn._remote)

    try:
        while not conn._closed:
            try:
                raw = await conn.recv()
            except (RudpTimeout, RudpReset):
                break
            if not raw:
                break

            try:
                msg = json.loads(raw.decode('utf-8'))
            except json.JSONDecodeError as e:
                log.warning("JSON error from %s: %s", conn._remote, e)
                await _send(conn, {"type": "error", "message": "invalid JSON"})
                continue

            msg_type = msg.get('type', '')
            req_id   = msg.get('req_id')

            if msg_type == 'hello':
                client_id = msg.get('client_id', 'unknown')
                rtt_ms    = float(msg.get('rtt_ms', 50.0))
                log.info("hello from client_id=%s rtt_ms=%.1f", client_id, rtt_ms)
                await _register_client_rtt(client_id, rtt_ms)
                await _update_client(client_id,
                    connected_at=int(time.time() * 1000),
                    rtt_ms=round(rtt_ms, 2),
                    submitted=False,
                    answers_count=0,
                )
                # Acknowledgement so client knows hello was received
                await _send(conn, {"type": "hello_ack", "client_id": client_id})

            elif msg_type == 'time_req':
                await _send(conn, {
                    "type": "time_resp",
                    "req_id": req_id,
                    "server_now_ms": int(time.time() * 1000),
                })

            elif msg_type == 'schedule_req':
                # Extract 3-sample RTT data sent by the client after its NTP phase.
                # These give better D_i than the RUDP handshake RTT alone.
                rtt_samples = msg.get('rtt_samples')
                if rtt_samples and len(rtt_samples) >= 3 and client_id:
                    await _register_client_rtt(client_id,
                                               rtt_samples[1],   # median as primary
                                               rtt_samples,
                                               offset_ms=msg.get('offset_ms'))
                # Reset coordinator so it is fresh for the upcoming exam_req wave
                _coordinator.reset()

                start_ms = await _get_start_at_ms()
                current_exam = _load_exam()
                exam_id = current_exam['exam_id']
                D       = _compute_D(client_id)
                delay   = _deliver_delay_ms(client_id)
                await _send(conn, {
                    "type":               "schedule_resp",
                    "req_id":             req_id,
                    "exam_id":            exam_id,
                    "start_at_server_ms": start_ms,
                    "duration_sec":       current_exam['duration_sec'],
                    # Sync metadata (bridge fine-tuning + UI display)
                    "deliver_delay_ms":   delay,
                    "max_rtt_ms":         round(_get_max_rtt_ms(), 2),
                    "my_rtt_ms":          round(_client_rtts.get(client_id, 50.0) if client_id else 50.0, 2),
                    "D_i":                round(D, 2),
                    "relay_candidate":    _relay_candidate(),
                })

            elif msg_type == 'exam_req':
                # ── Staggered send (friend's formula) ────────────────────
                # Register this client's D_i in the coordinator.
                # All exam_req handlers wait until the coordinator has seen
                # all clients (COLLECTION_WINDOW_SEC), then each sleeps until
                # its individual T_send time before sending the response.
                #
                # T_send[i] = T_arr - D_i
                # T_arr = now + max(D_i) + N*C + M
                #
                # Clients with high D_i (slow/jittery) get T_send earlier;
                # clients with low D_i (fast) get T_send later.
                # Result: all browsers receive the exam at the same T_arr.

                # ── Fresh RTT update (second NTP run, 5 s before T₀) ─────
                # The browser re-runs 3 NTP rounds just before the exam starts
                # and includes [min, med, max] rtt_samples + NTP offset_ms.
                # Updating here gives the coordinator the most accurate D_i
                # right when the staggered-send schedule is being computed.
                fresh_samples = msg.get('rtt_samples')
                fresh_offset  = msg.get('offset_ms')
                if fresh_samples and len(fresh_samples) >= 3 and client_id:
                    await _register_client_rtt(
                        client_id,
                        fresh_samples[1],          # median as primary RTT
                        fresh_samples,
                        offset_ms=fresh_offset,
                    )
                    log.info("exam_req: fresh RTT update for %s samples=%s offset=%s",
                             client_id, fresh_samples, fresh_offset)

                D = _compute_D(client_id)
                await _coordinator.register(client_id or 'unknown', D)
                t_send = await _coordinator.wait_t_send(client_id or 'unknown')

                # Sleep until scheduled send time
                wait_ms = t_send - time.time() * 1000
                if wait_ms > 0:
                    log.info("Stagger send: client=%s D=%.1fms waiting=%.1fms",
                             client_id, D, wait_ms)
                    await asyncio.sleep(wait_ms / 1000.0)

                current_exam = _load_exam()
                delay        = _deliver_delay_ms(client_id)
                await _send(conn, {
                    "type":              "exam_resp",
                    "req_id":            req_id,
                    "exam":              current_exam,
                    # Primary: server already staggered the send time (D_i formula)
                    # Secondary: bridge fine-tunes with self-correcting hold
                    "deliver_delay_ms":  delay,
                    "max_rtt_ms":        round(_get_max_rtt_ms(), 2),
                    "my_rtt_ms":         round(_client_rtts.get(client_id, 50.0) if client_id else 50.0, 2),
                    # Proof fields
                    "D_i":               round(D, 2),
                    "T_send_planned_ms": round(t_send, 2),
                    "stagger_wait_ms":   round(max(0.0, wait_ms), 2),
                })
                if client_id:
                    # exam_opened_at = when the exam became visible to this client (≈ T₀)
                    await _update_client(client_id,
                        exam_opened_at=int(time.time() * 1000),
                        total_questions=len(current_exam.get('questions', [])),
                    )

            elif msg_type == 'exam_begin':
                # Student clicked "Start" — record when they actually began answering
                opened_at_ms = msg.get('opened_at_ms', int(time.time() * 1000))
                log.info("exam_begin from client_id=%s opened_at_ms=%d", client_id, opened_at_ms)
                await _send(conn, {
                    "type": "exam_begin_ack",
                    "req_id": req_id,
                    "ok": True,
                })
                if client_id:
                    await _update_client(client_id,
                        exam_opened_at=opened_at_ms,           # client-reported T₀ receipt time
                        student_started_at=int(time.time() * 1000),  # when they clicked Start
                    )

            elif msg_type == 'answers_save':
                answers = msg.get('answers', [])
                eid = msg.get('exam_id', exam_id or 'unknown')
                _save_answers(eid, client_id or 'unknown', answers)
                await _send(conn, {
                    "type": "answers_saved",
                    "req_id": req_id,
                    "ok": True,
                })
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
                await _send(conn, {
                    "type": "submit_resp",
                    "req_id": req_id,
                    "ok": True,
                })
                if client_id:
                    await _update_client(client_id,
                        submitted=True,
                        submitted_at=int(time.time() * 1000),
                        answers_count=_count_answered(answers),
                    )

            else:
                await _send(conn, {
                    "type": "error",
                    "req_id": req_id,
                    "message": f"unknown message type: {msg_type}",
                })

    except Exception as e:
        log.exception("Connection error: %s", e)
    finally:
        await conn.close()
        log.info("Connection closed: client_id=%s", client_id)


async def _send(conn: RudpSocket, obj: dict):
    # Stamp every outgoing packet with server wall-clock time.
    # The bridge uses this to compute self-correcting hold times:
    #   hold_ms = (server_sent_at_ms + max_rtt/2 + buffer) - time.now()
    # Any in-flight delay (RUDP retransmission, queueing) is automatically
    # compensated because time.now() will have already advanced by that amount.
    obj['server_sent_at_ms'] = int(time.time() * 1000)
    data = json.dumps(obj).encode('utf-8')
    try:
        await conn.send(data)
    except (RudpTimeout, RudpReset) as e:
        log.warning("Send failed: %s", e)


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


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    server = RudpServer(host=host, port=RUDP_PORT)
    await server.start()
    log.info("Exam server listening on %s:%d (RUDP)", host, RUDP_PORT)

    while True:
        conn = await server.accept()
        asyncio.create_task(handle_connection(conn))


if __name__ == '__main__':
    asyncio.run(main())
