#!/usr/bin/env python3
"""
Headless exam client simulator.

Speaks the full exam WebSocket/JSON protocol against a running bridge.
Can be run on the host (using exposed ports) or copied inside a container.

Usage:
  python3 client_sim.py --ws ws://localhost:8081/ws --id student-A
  python3 client_sim.py --ws ws://localhost:8081/ws-tcp-sync  --id student-B
  python3 client_sim.py --ws ws://localhost:8081/ws --disconnect-after answers_save

Disconnect stages: hello | sync | schedule | exam_req | answers_save | submit
"""
import asyncio
import json
import sys
import time
import uuid
import argparse

# Ensure stdout can handle Unicode on Windows (CP1252 default would crash on arrows)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import websockets



STAGES = ["hello", "sync", "schedule", "exam_req", "answers_save", "submit"]


async def run(ws_url: str, client_id: str,
              disconnect_after: str | None, verbose: bool,
              think_ms: int = 0) -> dict:

    def log(msg: str):
        if verbose:
            print(f"[{client_id}] {msg}", flush=True)

    result: dict = {"client_id": client_id, "status": "incomplete"}

    async with websockets.connect(ws_url, open_timeout=15) as ws:

        # ── 0. connection_info ───────────────────────────────────────────────
        raw = await asyncio.wait_for(ws.recv(), 10)
        info = json.loads(raw)
        if info.get("type") == "error":
            raise RuntimeError(f"Bridge error: {info['message']}")
        if info.get("type") == "connection_info":
            log(f"connected  resolved_ip={info.get('resolved_ip')}  "
                f"handshake_rtt={info.get('handshake_rtt_ms')}ms")
            result["connection_info"] = {
                "resolved_ip": info.get("resolved_ip"),
                "handshake_rtt_ms": info.get("handshake_rtt_ms"),
            }

        # ── 1. hello ─────────────────────────────────────────────────────────
        await ws.send(json.dumps({"type": "hello", "client_id": client_id}))
        log("-> hello")
        # Consume hello_ack so it doesn't pollute the NTP recv() below
        ack = json.loads(await asyncio.wait_for(ws.recv(), 5))
        log(f"<- {ack.get('type')}")
        if disconnect_after == "hello":
            log("!! disconnecting after hello")
            return result

        # ── 2. NTP sync — 3 bootstrap rounds (50 ms cadence) ────────────────
        samples: list[dict] = []
        for i in range(3):
            req_id = uuid.uuid4().hex[:8]
            t0 = time.time() * 1000
            await ws.send(json.dumps(
                {"type": "time_req", "req_id": req_id, "client_t0_ms": t0}
            ))
            raw = await asyncio.wait_for(ws.recv(), 5)
            t1 = time.time() * 1000
            resp = json.loads(raw)
            rtt    = t1 - t0
            offset = resp["server_now_ms"] - (t0 + rtt / 2)
            samples.append({"rtt": rtt, "offset": offset})
            log(f"  NTP {i + 1}/3  rtt={rtt:.1f}ms  offset={offset:.1f}ms")
            if i < 2:
                await asyncio.sleep(0.05)

        best   = min(samples, key=lambda s: s["rtt"])
        rtt_s  = sorted(s["rtt"] for s in samples)
        result.update({"rtt_min": rtt_s[0], "rtt_med": rtt_s[1],
                        "rtt_max": rtt_s[2], "offset_ms": best["offset"]})

        if disconnect_after == "sync":
            log("!! disconnecting after sync")
            return result

        # ── 3. schedule_req ──────────────────────────────────────────────────
        await ws.send(json.dumps({
            "type": "schedule_req", "req_id": uuid.uuid4().hex[:8],
            "rtt_samples": rtt_s, "offset_ms": best["offset"],
        }))
        log("-> schedule_req")

        sched = None
        deadline = time.time() + 15
        while sched is None:
            if time.time() > deadline:
                raise TimeoutError("schedule_resp timeout")
            msg = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if msg.get("type") == "schedule_resp":
                sched = msg

        log(f"<- schedule_resp  exam={sched['exam_id']}  "
            f"start_at={sched['start_at_server_ms']}")
        result["exam_id"]          = sched["exam_id"]
        result["start_at_server_ms"] = sched["start_at_server_ms"]

        if disconnect_after == "schedule":
            log("!! disconnecting after schedule")
            return result

        # ── 4. Wait to T0 - 500 ms, then send exam_req ──────────────────────
        local_t0 = sched["start_at_server_ms"] - best["offset"]
        wait_ms  = local_t0 - time.time() * 1000 - 500
        if wait_ms > 500:
            log(f"  sleeping {wait_ms:.0f} ms -> T0-500ms ...")
            await asyncio.sleep(wait_ms / 1000)

        await ws.send(json.dumps({
            "type": "exam_req", "req_id": uuid.uuid4().hex[:8],
            "exam_id": sched["exam_id"],
            "rtt_samples": rtt_s, "offset_ms": best["offset"],
        }))
        log("-> exam_req")

        if disconnect_after == "exam_req":
            log("!! disconnecting after exam_req")
            return result

        # ── 5. exam_resp ─────────────────────────────────────────────────────
        exam_msg = None
        deadline  = time.time() + 30
        while exam_msg is None:
            if time.time() > deadline:
                raise TimeoutError("exam_resp timeout")
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if msg.get("type") == "exam_resp":
                exam_msg = msg

        fwd  = exam_msg.get("bridge_forwarded_at_ms", 0)
        hold = exam_msg.get("sync_hold_ms")
        tgt  = exam_msg.get("sync_target_ms", 0)
        delta = round(fwd - tgt, 2) if fwd and tgt else None
        log(f"<- exam_resp  forwarded_at={fwd}  hold={hold}ms  delta={delta}ms")

        result.update({
            "bridge_forwarded_at_ms": fwd,
            "sync_hold_ms": hold,
            "sync_target_ms": tgt,
            "sync_delta_ms": delta,
        })

        # ── 6. answers_save ──────────────────────────────────────────────────
        answers: dict = {}
        for q in exam_msg.get("exam", {}).get("questions", []):
            qid = q["id"]
            if q.get("options"):
                answers[qid] = q["options"][0]["id"]
            else:
                answers[qid] = "simulated-answer"

        # Simulated reading/thinking time so the pcap shows a visible gap
        # between the large exam download and the answer submission.
        if think_ms > 0:
            log(f"  thinking for {think_ms} ms (simulated exam time) …")
            await asyncio.sleep(think_ms / 1000.0)

        await ws.send(json.dumps({
            "type": "answers_save",
            "exam_id": sched["exam_id"],
            "answers": answers,
        }))
        log("-> answers_save")

        if disconnect_after == "answers_save":
            log("!! disconnecting after answers_save  (simulating mid-exam dropout)")
            return result

        saved = json.loads(await asyncio.wait_for(ws.recv(), 5))
        log(f"<- {saved.get('type')}")

        # ── 7. submit_req ────────────────────────────────────────────────────
        await ws.send(json.dumps({
            "type": "submit_req", "req_id": uuid.uuid4().hex[:8],
            "exam_id": sched["exam_id"], "answers": answers,
        }))
        log("-> submit_req")

        if disconnect_after == "submit":
            log("!! disconnecting before submit_resp")
            return result

        sub = json.loads(await asyncio.wait_for(ws.recv(), 8))
        log(f"<- {sub.get('type')}  received_at={sub.get('received_at_ms')}")
        result["submit_resp"] = sub
        result["status"]      = "complete"

    return result


async def main() -> None:
    ap = argparse.ArgumentParser(description="Headless exam client simulator")
    ap.add_argument("--ws",     default="ws://localhost:8081/ws",
                    help="WebSocket URL (default: ws://localhost:8081/ws)")
    ap.add_argument("--id",     default=None, dest="client_id",
                    help="Client ID (auto-generated if omitted)")
    ap.add_argument("--disconnect-after", default=None,
                    choices=STAGES, dest="disconnect_after",
                    help="Disconnect after this stage (for disconnect scenario)")
    ap.add_argument("--think-ms", default=0, type=int, dest="think_ms",
                    help="Simulated think/answer time in ms between exam_resp and submit")
    ap.add_argument("--quiet",  action="store_true",
                    help="Suppress per-step log lines")
    args = ap.parse_args()

    cid = args.client_id or f"sim-{uuid.uuid4().hex[:6]}"
    try:
        result = await run(args.ws, cid, args.disconnect_after,
                           verbose=not args.quiet, think_ms=args.think_ms)
    except Exception as exc:
        result = {"client_id": cid, "status": "error", "error": str(exc)}

    # Always print JSON result as last line so run_scenarios.py can parse it
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
