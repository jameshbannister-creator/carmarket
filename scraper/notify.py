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


def short_name(name: str) -> str:
    replacements = {
        "Mercedes-Benz E55 AMG (W210)": "E55",
        "Alfa Romeo 156 GTA": "156 GTA",
        "Toyota RAV4 Mk1": "RAV4 Mk1",
    }
    return replacements.get(name, name.split()[-1])


def build_digest(data: dict) -> tuple[str, str, str]:
    models = data.get("models", [])
    alerts = data.get("alerts", [])
    parts = []

    for m in models:
        supply = m["supply"]["current"]
        price = m["price"]["median"]
        signal = m.get("signal", "stable")
        emoji = SIGNAL_EMOJI.get(signal, "–")
        price_str = f"£{price:,}" if price else "—"
        parts.append(f"{short_name(m['name'])}: {supply} · {price_str} {emoji}")

    title = "Carmarket"
    body = "  |  ".join(parts)

    subtitle = ""
    if alerts:
        subtitle = alerts[0]["text"].replace("<strong>", "").replace("</strong>", "")

    return title, body, subtitle


def notify(title: str, body: str, subtitle: str) -> None:
    sub_clause = f'subtitle "{subtitle}" ' if subtitle else ""
    script = f'display notification "{body}" with title "{title}" {sub_clause}'
    subprocess.run(["osascript", "-e", script], check=False)
    print(f"[notify] {title}: {body}")
    if subtitle:
        print(f"[notify] {subtitle}")


def main() -> None:
    if not DATA_FILE.exists():
        print("[notify] latest.json not found — skipping")
        return
    data = json.loads(DATA_FILE.read_text())
    title, body, subtitle = build_digest(data)
    notify(title, body, subtitle)


if __name__ == "__main__":
    main()
