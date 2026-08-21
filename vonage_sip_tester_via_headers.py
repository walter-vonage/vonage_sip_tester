#!/usr/bin/env python3
"""
Vonage SIP Trunk Inbound Call Tester
=====================================
Listens for SIP INVITEs over TCP and responds with a configurable SIP status
code. Designed to reproduce the retry-on-rejection bug in the PT call tracking
/ voice proxy use case reported via Simon.

Expose publicly using ngrok:
    ngrok tcp 5060
    → copy the forwarded address (e.g. 0.tcp.eu.ngrok.io:12345)
    → enter it as the SIP URI in the Vonage SIP Trunk dashboard (TCP transport)

Usage:
    python3 vonage_sip_tester.py                # respond 404 (default)
    python3 vonage_sip_tester.py --code 603     # respond 603 Decline
    python3 vonage_sip_tester.py --code 503     # baseline: should allow failover

All traffic is written to vonage_sip_tester.log in the current directory.
"""

import socket
import threading
import re
import datetime
import random
import string
import os
import argparse

# ─── Configuration ────────────────────────────────────────────────────────────
DEFAULT_CODE = 404
DEFAULT_PORT = 5060
LOG_FILE     = "vonage_sip_tester.log"
# ─────────────────────────────────────────────────────────────────────────────

SIP_REASON = {
    200: "OK",
    404: "Not Found",
    480: "Temporarily Unavailable",
    486: "Busy Here",
    500: "Server Internal Error",
    503: "Service Unavailable",
    603: "Decline",
}

# ── Thread-safe logging ───────────────────────────────────────────────────────

_log_lock = threading.Lock()

def log(msg):
    ts   = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    line = f"[{ts}] {msg}"
    with _log_lock:
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")

def log_raw(label, raw_msg):
    divider = "─" * 50
    with _log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{divider}\n{label}\n{divider}\n{raw_msg}\n{divider}\n\n")

# ── SIP helpers ───────────────────────────────────────────────────────────────

def random_tag(n=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def sip_header(msg, name):
    m = re.search(rf'^{re.escape(name)}\s*:\s*(.+)$', msg, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else None

def sip_headers_all(msg, name):
    """Return ALL values for a repeated header (Via can appear multiple times)."""
    pattern = re.compile(rf'^{re.escape(name)}\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
    return [m.group(1).strip() for m in pattern.finditer(msg)]

def sip_method(msg):
    first = msg.split("\n")[0].strip()
    word  = first.split(" ", 1)[0]
    return word if word in {"INVITE","ACK","BYE","CANCEL","OPTIONS","REGISTER","INFO"} else None

def build_response(request, code):
    reason  = SIP_REASON.get(code, "Unknown")
    # RFC 3261 §12.1.1: ALL Via headers from the request must be echoed back
    # in the same order so proxies can route the response correctly.
    vias    = sip_headers_all(request, "Via") or sip_headers_all(request, "v")
    frm     = sip_header(request, "From") or sip_header(request, "f") or ""
    to      = sip_header(request, "To")   or sip_header(request, "t") or ""
    call_id = sip_header(request, "Call-ID") or sip_header(request, "i") or ""
    cseq    = sip_header(request, "CSeq") or ""
    if ";tag=" not in to:
        to += f";tag={random_tag()}"
    if not vias:
        log("    WARNING: No Via headers found in INVITE — response will be invalid!")
    via_block = "".join(f"Via: {v}\r\n" for v in vias)
    response = (
        f"SIP/2.0 {code} {reason}\r\n"
        f"{via_block}"
        f"From: {frm}\r\n"
        f"To: {to}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )
    return response

# ── Retry detection ───────────────────────────────────────────────────────────

_tracker_lock  = threading.Lock()
_invite_tracker: dict[str, list[str]] = {}  # call_id → [cseq, ...]

def track_invite(call_id, cseq):
    seq = cseq.split()[0] if cseq else "?"
    with _tracker_lock:
        _invite_tracker.setdefault(call_id, []).append(seq)
        count = len(_invite_tracker[call_id])
        prev  = list(_invite_tracker[call_id][:-1])
    return count, count > 1, prev

# ── TCP message reader ────────────────────────────────────────────────────────

def read_sip_messages(conn):
    """
    Generator: yields complete SIP messages from a TCP stream.
    Handles Content-Length framing per RFC 3261.
    """
    buf = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return
        buf += chunk
        while True:
            sep = buf.find(b"\r\n\r\n")
            if sep == -1:
                break
            headers_bytes = buf[:sep]
            headers_text  = headers_bytes.decode("utf-8", errors="replace")
            cl_match = re.search(r"Content-Length\s*:\s*(\d+)", headers_text, re.IGNORECASE)
            cl       = int(cl_match.group(1)) if cl_match else 0
            total    = sep + 4 + cl
            if len(buf) < total:
                break
            yield buf[:total].decode("utf-8", errors="replace")
            buf = buf[total:]

# ── Connection handler ────────────────────────────────────────────────────────

_stats_lock    = threading.Lock()
_invite_total  = 0
_retry_total   = 0

def handle_connection(conn, peer, response_code):
    global _invite_total, _retry_total
    try:
        for msg in read_sip_messages(conn):
            method = sip_method(msg)
            if not method:
                continue

            frm     = sip_header(msg, "From") or sip_header(msg, "f") or "?"
            to      = sip_header(msg, "To")   or sip_header(msg, "t") or "?"
            call_id = sip_header(msg, "Call-ID") or sip_header(msg, "i") or "?"
            cseq    = sip_header(msg, "CSeq") or "?"

            # ── INVITE ──────────────────────────────────────────────────────
            if method == "INVITE":
                with _stats_lock:
                    _invite_total += 1
                    n = _invite_total

                inv_n, is_retry, prev_seqs = track_invite(call_id, cseq)
                retry_flag = "  *** RETRY DETECTED ***" if is_retry else ""

                log(f">>> INVITE #{n} from {peer[0]}:{peer[1]}{retry_flag}")
                log(f"    Call-ID  : {call_id}")
                log(f"    From     : {frm}")
                log(f"    To       : {to}")
                log(f"    CSeq     : {cseq}")
                if is_retry:
                    with _stats_lock:
                        _retry_total += 1
                    log(f"    Previous CSeqs for this Call-ID: {', '.join(prev_seqs)}")
                log_raw(f"RAW INVITE #{n}", msg)

                resp = build_response(msg, response_code)
                conn.sendall(resp.encode())
                log(f"<<< SENT {response_code} {SIP_REASON.get(response_code,'?')} → {peer[0]}:{peer[1]}")
                # Log the Via headers that were included so we can verify
                via_lines = [l for l in resp.split("\r\n") if l.lower().startswith("via:")]
                for vl in via_lines:
                    log(f"    Sent {vl}")
                log_raw(f"RAW RESPONSE #{n}", resp)
                log("")

            # ── ACK ─────────────────────────────────────────────────────────
            elif method == "ACK":
                log(f"    ACK  Call-ID={call_id}  (Vonage accepted the final response)")
                log("")

            # ── OPTIONS keepalive ────────────────────────────────────────────
            elif method == "OPTIONS":
                ok = (
                    f"SIP/2.0 200 OK\r\n"
                    f"Via: {sip_header(msg,'Via') or ''}\r\n"
                    f"From: {sip_header(msg,'From') or ''}\r\n"
                    f"To: {sip_header(msg,'To') or ''};tag={random_tag()}\r\n"
                    f"Call-ID: {call_id}\r\n"
                    f"CSeq: {cseq}\r\n"
                    f"Content-Length: 0\r\n\r\n"
                )
                conn.sendall(ok.encode())
                log(f"    OPTIONS keepalive from {peer[0]} → 200 OK")
                log("")

            # ── CANCEL / BYE ─────────────────────────────────────────────────
            elif method in ("CANCEL", "BYE"):
                conn.sendall(build_response(msg, 200).encode())
                log(f"    {method}  Call-ID={call_id} → 200 OK")
                log("")

    except Exception as exc:
        log(f"    Connection error from {peer}: {exc}")
    finally:
        conn.close()

# ── Main server ───────────────────────────────────────────────────────────────

def run(response_code: int, listen_port: int):
    reason = SIP_REASON.get(response_code, "Unknown")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(20)

    log("=" * 60)
    log("  Vonage SIP Trunk Inbound Call Tester")
    log("=" * 60)
    log(f"  Transport      : TCP")
    log(f"  Listening on   : 0.0.0.0:{listen_port}")
    log(f"  Response code  : {response_code} {reason}")
    log(f"  Log file       : {os.path.abspath(LOG_FILE)}")
    log(f"  Press Ctrl+C to stop")
    log("=" * 60)
    log("")

    try:
        while True:
            conn, peer = srv.accept()
            t = threading.Thread(
                target=handle_connection, args=(conn, peer, response_code), daemon=True
            )
            t.start()
    except KeyboardInterrupt:
        log("")
        log("=" * 60)
        log("  Test session ended")
        log(f"  Total INVITEs received : {_invite_total}")
        log(f"  Retries detected       : {_retry_total}")
        if _retry_total > 0:
            log("  ✗  BUG CONFIRMED — Vonage retried after a definitive rejection")
        elif _invite_total > 0:
            log("  ✓  No retries — expected behaviour for this response code")
        else:
            log("  (no calls received)")
        log(f"  Full log               : {os.path.abspath(LOG_FILE)}")
        log("=" * 60)
    finally:
        srv.close()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Vonage SIP Trunk Inbound Call Tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 vonage_sip_tester.py                # 404 Not Found (default)
  python3 vonage_sip_tester.py --code 603     # 603 Decline
  python3 vonage_sip_tester.py --code 503     # 503 Service Unavailable (failover baseline)

Supported codes: 200, 404, 480, 486, 500, 503, 603
        """,
    )
    parser.add_argument("--code", type=int, default=DEFAULT_CODE,
                        help=f"SIP response code for every INVITE (default: {DEFAULT_CODE})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    if args.code not in SIP_REASON:
        print(f"Warning: {args.code} not in known list {list(SIP_REASON.keys())}")

    run(args.code, args.port)
