"""Bind email attachments to a successfully completed report generation."""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")
MANIFEST_NAME = "ETF_analyst_report.delivery.json"


def write_delivery_manifest(report_path: Path) -> None:
    report_path = Path(report_path)
    payload = {
        "filename": report_path.name,
        "generated_at": datetime.now(TORONTO).isoformat(),
        "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    }
    target = report_path.parent / MANIFEST_NAME
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_delivery(report_dir: Path):
    """Return verified bytes; never relabel an older report as today's report."""
    try:
        payload = json.loads((report_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        if payload["filename"] != "ETF_analyst_report.xlsx":
            raise ValueError("unexpected report filename")
        generated = datetime.fromisoformat(payload["generated_at"])
        now = datetime.now(TORONTO)
        if generated.tzinfo is None or generated.astimezone(TORONTO).date() != now.date():
            raise ValueError("report was not generated today in Toronto")
        if generated > now:
            raise ValueError("report generation time is in the future")
        for key, env in (("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT")):
            if os.environ.get(env) and payload.get(key) != os.environ[env]:
                raise ValueError("report belongs to another workflow run or attempt")
        report_path = report_dir / payload["filename"]
        data = report_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise ValueError("report checksum mismatch")
        return report_path, data, generated.astimezone(TORONTO)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Report delivery validation failed: {exc}") from exc
