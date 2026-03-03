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
# Strategy: server stamps each content packet with deliver_delay_ms.
# Fast clients (low RTT) wait longer; slow clients get it immediately.
# Result: all clients receive the packet at the same wall-clock moment.

_client_rtts: Dict[str, float] = {}   # client_id → EWMA RTT in ms
_rtt_lock = asyncio.Lock()
JITTER_BUFFER_MS = 30                  # extra slack on top of max RTT


async def _register_client_rtt(client_id: str, rtt_ms: float):
    async with _rtt_lock:
        _client_rtts[client_id] = rtt_ms
    log.info("RTT registry: client=%s rtt=%.1fms  all=%s",
             client_id, rtt_ms,
             {k: round(v, 1) for k, v in _client_rtts.items()})


def _get_max_rtt_ms() -> float:
    return max(_client_rtts.values(), default=100.0)


def _deliver_delay_ms(client_id: Optional[str]) -> int:
    """
    How many ms this client's bridge should hold the packet before forwarding.

    Derivation (assuming symmetric RTT):
      - Server sends packet at T_send.
      - Bridge receives it at T_send + rtt/2  (one-way latency).
      - We want every client's browser to receive at T_send + max_rtt/2 + BUFFER.
      - Therefore: deliver_delay = (max_rtt - my_rtt) / 2 + BUFFER

    The slowest client waits only BUFFER ms; the fastest client waits
    (max_rtt - my_rtt)/2 + BUFFER, so all browsers see the packet at the
    same wall-clock instant.
    """
    if not client_id or client_id not in _client_rtts:
        return 0
    max_rtt = _get_max_rtt_ms()
    my_rtt  = _client_rtts[client_id]
    delay   = max(0, (max_rtt - my_rtt) / 2 + JITTER_BUFFER_MS)
    return int(delay)


def _relay_candidate() -> Optional[str]:
    """Client with lowest RTT — best relay candidate for future optimisation."""
    if len(_client_rtts) < 2:
        return None
    return min(_client_rtts, key=lambda k: _client_rtts[k])

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

            elif msg_type == 'time_req':
                await _send(conn, {
                    "type": "time_resp",
                    "req_id": req_id,
                    "server_now_ms": int(time.time() * 1000),
                })

            elif msg_type == 'schedule_req':
                start_ms = await _get_start_at_ms()
                current_exam = _load_exam()
                exam_id = current_exam['exam_id']
                delay   = _deliver_delay_ms(client_id)
                await _send(conn, {
                    "type":               "schedule_resp",
                    "req_id":             req_id,
                    "exam_id":            exam_id,
                    "start_at_server_ms": start_ms,
                    "duration_sec":       current_exam['duration_sec'],
                    # Synchronized delivery metadata
                    "deliver_delay_ms":   delay,
                    "max_rtt_ms":         round(_get_max_rtt_ms(), 2),
                    "my_rtt_ms":          round(_client_rtts.get(client_id, 50.0) if client_id else 50.0, 2),
                    "relay_candidate":    _relay_candidate(),
                })

            elif msg_type == 'exam_req':
                # Load fresh from disk so admin-uploaded exam takes effect
                current_exam = _load_exam()
                delay = _deliver_delay_ms(client_id)
                await _send(conn, {
                    "type":             "exam_resp",
                    "req_id":           req_id,
                    "exam":             current_exam,
                    # Synchronized delivery — bridge holds this packet until
                    # deliver_delay_ms elapses so all clients see exam at once
                    "deliver_delay_ms": delay,
                    "max_rtt_ms":       round(_get_max_rtt_ms(), 2),
                    "my_rtt_ms":        round(_client_rtts.get(client_id, 50.0) if client_id else 50.0, 2),
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
