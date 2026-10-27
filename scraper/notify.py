"""
Post-scrape digest: reads latest.json and fires a macOS notification.
Run after run_all.py completes.
"""

import json
import subprocess
from pathlib import Path

DATA_FILE = Path(__file__).parent.parent / "data" / "latest.json"

SIGNAL_EMOJI = {
    "hot": "🔥",
    "rising": "📈",
    "watch": "👀",
    "stable": "–",
}


def build_digest(data: dict) -> tuple[str, str]:
    models = data.get("models", [])
    lines = []
    alerts = data.get("alerts", [])

    for m in models:
        name = m["name"]
        supply = m["supply"]["current"]
        price = m["price"]["median"]
        signal = m.get("signal", "stable")
        emoji = SIGNAL_EMOJI.get(signal, "–")

        price_str = f"£{price:,}" if price else "—"
        lines.append(f"{emoji} {name}: {supply} listed · {price_str}")

    title = "Carmarket weekly update"
    body = "\n".join(lines)
    if alerts:
        body += "\n\n" + " | ".join(
            a["text"].replace("<strong>", "").replace("</strong>", "")
            for a in alerts
        )
    return title, body


def notify(title: str, body: str) -> None:
    # Single-line for the notification banner; full digest to stdout
    banner = body.split("\n")[0] + (" …" if "\n" in body else "")
    script = (
        f'display notification "{body}" with title "{title}" '
        f'subtitle "{banner}"'
    )
    subprocess.run(["osascript", "-e", script], check=False)
    print(f"[notify] {title}")
    print(body)


def main() -> None:
    if not DATA_FILE.exists():
        print("[notify] latest.json not found — skipping")
        return
    data = json.loads(DATA_FILE.read_text())
    title, body = build_digest(data)
    notify(title, body)


if __name__ == "__main__":
    main()
