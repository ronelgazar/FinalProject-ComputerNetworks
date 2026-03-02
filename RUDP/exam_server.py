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
                log.info("hello from client_id=%s", client_id)
                await _update_client(client_id,
                    connected_at=int(time.time() * 1000),
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
                await _send(conn, {
                    "type": "schedule_resp",
                    "req_id": req_id,
                    "exam_id": exam_id,
                    "start_at_server_ms": start_ms,
                    "duration_sec": current_exam['duration_sec'],
                })

            elif msg_type == 'exam_req':
                # Load fresh from disk so admin-uploaded exam takes effect
                current_exam = _load_exam()
                await _send(conn, {
                    "type": "exam_resp",
                    "req_id": req_id,
                    "exam": current_exam,
                })
                if client_id:
                    await _update_client(client_id,
                        exam_started_at=int(time.time() * 1000),
                        total_questions=len(current_exam.get('questions', [])),
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
