# Diagrams

One PlantUML file per system aspect.

| File | Topic |
|------|-------|
| `01_dhcp.puml` | DHCP 4-way handshake (DISCOVER → OFFER → REQUEST → ACK) |
| `02_dns_iterative.puml` | DNS iterative resolution: ws_bridge → Resolver → Root → TLD → Auth |
| `03_dns_recursive.puml` | DNS recursive mode (RECURSIVE_CAPABLE=1), comparison with iterative |
| `04_rudp_handshake.puml` | RUDP 3-way handshake, header format, handshake_rtt_ms measurement |
| `05_rudp_sliding_window.puml` | RUDP sliding window: chunking, Slow Start, AIMD, Fast Retransmit, Timeout |
| `06_rtt_sync.puml` | RTT sync: Cristian algorithm (rounds 1–4) + Adaptive EWMA (rounds 5–12) |
| `07_exam_coordinator.puml` | ExamSendCoordinator: D_i computation, T_send stagger, ZS wire format |
| `08_bridge_hold.puml` | Bridge self-correcting hold: ZS decode, target_ms, hold_ms, SyncProofCard |
| `09_transport_modes.puml` | Three transport modes: RUDP+Sync vs TCP+Sync vs TCP+NoSync |
| `10_client_lifecycle.puml` | Full client state machine from boot to exam submission |

## Render

Open any file in a PlantUML-aware editor (VS Code + PlantUML extension, IntelliJ, or online at plantuml.com).
