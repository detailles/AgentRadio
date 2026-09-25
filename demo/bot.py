#!/usr/bin/env python3
"""Demo agent: joins Radio as <handle>, prints incoming deliveries, and
auto-replies to PMs. Run it inside a Herdr pane:

    python3 demo/bot.py bot1
"""

import os
import re
import subprocess
import sys

RADIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "radio")


def radio(*args: str) -> None:
    """Run the radio CLI with the current interpreter; raises on failure."""
    subprocess.run([sys.executable, RADIO, *args], check=True)


def main() -> int:
    """Join the given handle, then answer every [RADIO_MESSAGE] envelope that
    arrives on stdin — a minimal shell-pane agent loop to demo radio."""
    if len(sys.argv) != 2:
        print("usage: bot.py <handle>", file=sys.stderr)
        return 2
    handle = sys.argv[1]
    radio("join", handle, "--no-launch")
    print(f"[{handle}] listening — send me a radio pm", flush=True)
    # Deliveries reach shell panes flattened to one line:
    # [RADIO_MESSAGE id=N kind=pm from=X to=Y reply=...] Radio PM from X
    # [Reply required: ...] <text> [END_RADIO_MESSAGE id=N]
    envelope = re.compile(r"\[RADIO_MESSAGE (.*?)\](.*?)\[END_RADIO_MESSAGE id=\d+\]")
    for line in sys.stdin:
        line = line.strip()
        match = envelope.search(line)
        if not match:
            if line:
                print(f"[{handle}] ignored: {line}", flush=True)
            continue
        attrs = dict(
            part.split("=", 1) for part in match.group(1).split() if "=" in part
        )
        sender = attrs.get("from", "")
        text = match.group(2).strip()
        text = re.sub(rf"^Radio (PM from {re.escape(sender)}|message from {re.escape(sender)} in #\S+)", "", text).strip()
        text = text.removeprefix("Reply required: answer this over radio.").strip()
        print(f"[{handle}] got from {sender} (reply={attrs.get('reply')}): {text}", flush=True)
        if not sender or not text or text.startswith("ack from "):
            continue  # never ack an ack
        radio("pm", sender, f"ack from {handle}: {text}", "--from", handle)
        print(f"[{handle}] replied to {sender}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
