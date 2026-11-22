"""Daily Ball League run for GitHub Actions (free): render the next match and publish it at 06:00 Lisbon time.

The video is uploaded ahead of time as private with publishAt = next 06:00 Europe/Lisbon, so YouTube
itself makes it public at exactly 06:00 (summer and winter time handled). If the run is late (after
06:00), it is published immediately.

State lives in the repository and is committed back by the workflow:
    league.json     season table (written by make_video.render)
    published.json  one entry per published match: video id, sha256, seed, publish date

Guards (nothing is ever published twice):
    - one video per Lisbon day: skip if published.json already has the target date
    - match number already on the channel (read from the 'Match #N' line of the descriptions)
    - same file hash or same race seed as any published match

    python cloud_daily.py [--dry-run]
"""
import argparse
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.http import MediaFileUpload

import make_video as mv
import upload as up

ROOT = Path(__file__).resolve().parent
PUBLISHED = ROOT / "published.json"
LISBON = ZoneInfo("Europe/Lisbon")
HOUR = 6


def target_slot(now, days):
    """Today's 06:00 Lisbon if today has no video yet (published at once if 06:00 already passed),
    otherwise tomorrow's 06:00. The workflow runs every hour, so tomorrow's video is scheduled well ahead."""
    slot = now.astimezone(LISBON).replace(hour=HOUR, minute=0, second=0, microsecond=0)
    if slot.date().isoformat() in days:
        slot += datetime.timedelta(days=1)
    return slot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    pub = json.loads(PUBLISHED.read_text(encoding="utf-8")) if PUBLISHED.exists() else {}
    slot = target_slot(now, {d.get("publish_date") for d in pub.values()})
    day = slot.date().isoformat()
    done_day = [m for m, d in pub.items() if d.get("publish_date") == day]
    if done_day:
        print(f"nothing to do: match {done_day[0]} is already set for {day} 06:00 Lisbon")
        return

    yt = up.service(interactive=False)
    on_channel = up.channel_match_numbers(yt)
    state = mv.load_state()
    n = max(map(int, state["matches"]), default=0) + 1
    if n in on_channel or str(n) in pub:
        raise SystemExit(f"STOP: match #{n} is already on the channel / in published.json — state out of sync, "
                         "nothing uploaded.")
    if a.dry_run:
        print(f"WOULD render and publish match #{n} at {slot.isoformat()} (on channel: {sorted(on_channel)})")
        return

    mv.render(n)
    folder = mv.OUT / f"match_{n:03d}"
    video = folder / "video.mp4"
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    digest = up.sha256(video)
    seed = json.loads(mv.STATE.read_text(encoding="utf-8"))["matches"][str(n)]["seed"]
    for m, d in pub.items():
        if d.get("sha256") == digest or d.get("seed") == seed:
            raise SystemExit(f"SKIPPED DUPLICATE: match #{n} matches published match #{m} (youtu.be/{d['id']}).")

    status = {"privacyStatus": "public", "selfDeclaredMadeForKids": meta["madeForKids"],
              "containsSyntheticMedia": False}
    if slot > now + datetime.timedelta(minutes=15):
        status["privacyStatus"] = "private"
        status["publishAt"] = slot.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    body = {"snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": meta["tags"], "categoryId": meta["categoryId"]},
            "status": status}
    res = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True)).execute()
    pub[str(n)] = {"id": res["id"], "url": f"https://youtube.com/shorts/{res['id']}", "sha256": digest,
                   "seed": seed, "publish_date": day, "publishAt": status.get("publishAt") or "immediately",
                   "uploaded": now.isoformat()}
    PUBLISHED.write_text(json.dumps(pub, indent=2), encoding="utf-8")
    print(f"match #{n} uploaded: {pub[str(n)]['url']} -> public at "
          f"{status.get('publishAt') and slot.strftime('%Y-%m-%d %H:%M Lisbon') or 'now'}")


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        _s.reconfigure(encoding="utf-8")
    main()
