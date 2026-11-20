"""Upload rendered Ball League matches to YouTube (free YouTube Data API v3).

One-time setup (see README.md): put your OAuth client file here as client_secret.json.
First run opens the browser so YOU sign in and allow access; the token is saved to token.json.

Usage:
    python upload.py                     # upload every rendered match not uploaded yet, public now
    python upload.py --schedule 18:00    # schedule them one per day at 18:00 (local time), starting today/tomorrow
"""
import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
COMMENT_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"   # needed only to post the engagement comment
# Sign-in asks only for upload + read. The comments scope (force-ssl) triggers Google's strongest
# "unverified app" block for unverified apps, so it is not requested; the question goes in the
# video and description instead (see make_video.metadata).
AUTH_SCOPES = SCOPES
GRANTED = set()                             # scopes the saved token actually has
COMMENT = ("Which color did you pick BEFORE the race started? Be honest 👀👇\n"
           "Every match counts for the season table — new race every day!")
CHANNEL_ID = "UChn-MGHHRkCYVVFn_5tpZYw"  # Ball League (@BallLeagueDaily)


REAUTH = ("AUTORIZACAO EXPIRADA OU EM FALTA: corre 'python check_api.py' na pasta ball-league e "
          "escolhe o canal Ball League. Os videos por enviar seguem no proximo envio.")


def service(interactive=None):
    """interactive=False (scheduled runs) never opens a browser: it fails fast with REAUTH instead of hanging."""
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    tok = ROOT / "token.json"
    creds = Credentials.from_authorized_user_file(str(tok)) if tok.exists() else None
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except Exception as exc:  # invalid_grant: token revoked/expired
                print(f"refresh failed: {exc}")
        if not refreshed:
            if not interactive:
                raise SystemExit(REAUTH)
            secret = ROOT / "client_secret.json"
            if not secret.exists():
                raise SystemExit("Falta client_secret.json nesta pasta. Segue a secção 'Configurar a API' do README.md.")
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), AUTH_SCOPES)
            creds = flow.run_local_server(port=0)
        tok.write_text(creds.to_json(), encoding="utf-8")
    yt = build("youtube", "v3", credentials=creds)
    me = yt.channels().list(part="id,snippet", mine=True).execute()["items"][0]
    if me["id"] != CHANNEL_ID:
        raise SystemExit(f"Signed in to channel {me['id']} ({me['snippet']['title']}), expected {CHANNEL_ID}. "
                         "Delete token.json and sign in with the right channel.")
    GRANTED.clear()
    GRANTED.update(creds.scopes or [])
    return yt


def post_comment(yt, video_id):
    """Top-level comment from the channel to start the conversation. The API cannot pin comments."""
    if COMMENT_SCOPE not in GRANTED:
        return None
    try:
        res = yt.commentThreads().insert(part="snippet", body={"snippet": {
            "videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": COMMENT}}}}).execute()
        return res["id"]
    except Exception as exc:  # never let the comment break the daily upload
        print(f"  comment failed: {exc}")
        return None


def pending():
    for folder in sorted(OUT.glob("match_*")):
        if (folder / "video.mp4").exists() and (folder / "metadata.json").exists() \
                and not (folder / "uploaded.json").exists():
            yield folder


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def published_fingerprints():
    """Hash, match number and seed of every video already uploaded (backfills missing hashes)."""
    league = json.loads((ROOT / "league.json").read_text(encoding="utf-8"))["matches"]
    done = {}
    for up in OUT.glob("match_*/uploaded.json"):
        folder = up.parent
        info = json.loads(up.read_text(encoding="utf-8"))
        if "sha256" not in info and (folder / "video.mp4").exists():
            info["sha256"] = sha256(folder / "video.mp4")
            up.write_text(json.dumps(info, indent=2), encoding="utf-8")
        n = int(folder.name.split("_")[1])
        done[folder.name] = {"id": info["id"], "sha256": info.get("sha256"), "match": n,
                             "seed": league.get(str(n), {}).get("seed")}
    return done


def channel_match_numbers(yt):
    """Match numbers already on the channel, read from the 'Match #N' line of each description."""
    ch = yt.channels().list(part="contentDetails", id=CHANNEL_ID).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    found, token = {}, None
    while True:
        r = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=50, pageToken=token).execute()
        for it in r.get("items", []):
            m = re.search(r"Match #(\d+)\b", it["snippet"].get("description", ""))
            if m:
                found[int(m.group(1))] = it["snippet"]["resourceId"]["videoId"]
        token = r.get("nextPageToken")
        if not token:
            return found


def duplicate_reason(folder, done, on_channel):
    """None if the video is new; otherwise why it must NOT be uploaded."""
    n = int(folder.name.split("_")[1])
    seed = json.loads((ROOT / "league.json").read_text(encoding="utf-8"))["matches"].get(str(n), {}).get("seed")
    digest = sha256(folder / "video.mp4")
    for name, d in done.items():
        if d["sha256"] == digest:
            return f"same file as {name} (youtu.be/{d['id']})"
        if seed is not None and d["seed"] == seed:
            return f"same race (seed {seed}) as {name} (youtu.be/{d['id']})"
    if n in on_channel:
        return f"match #{n} is already on the channel (youtu.be/{on_channel[n]})"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", help="HH:MM local time; publish one video per day at this time")
    ap.add_argument("--limit", type=int, default=99)
    ap.add_argument("--unattended", action="store_true", help="never open a browser (scheduled runs)")
    ap.add_argument("--dry-run", action="store_true", help="only show what would be uploaded; upload nothing")
    a = ap.parse_args()
    yt = service(interactive=False if a.unattended else None)
    done = published_fingerprints()
    on_channel = channel_match_numbers(yt)
    print(f"already published: {', '.join(sorted(done)) or 'none'} | on channel: matches {sorted(on_channel) or 'none'}")
    queue = list(pending())
    if not queue:
        print("nothing to upload")
    slot = None
    if a.schedule:
        hh, mm = map(int, a.schedule.split(":"))
        now = datetime.datetime.now().astimezone()
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now + datetime.timedelta(minutes=20):
            slot += datetime.timedelta(days=1)
    for n, folder in enumerate(queue):
        if n >= a.limit:
            break
        meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        why = duplicate_reason(folder, done, on_channel)
        if why:
            print(f"SKIPPED DUPLICATE {folder.name}: {why}. Not uploaded.")
            continue
        print(f"{'WOULD UPLOAD' if a.dry_run else 'uploading'} {folder.name}: \"{meta['title']}\" (new: hash, race and match number all unique)")
        if a.dry_run:
            continue
        status = {"privacyStatus": "public", "selfDeclaredMadeForKids": meta["madeForKids"],
                  "containsSyntheticMedia": False}
        if slot:
            status["privacyStatus"] = "private"
            status["publishAt"] = slot.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        body = {"snippet": {"title": meta["title"], "description": meta["description"],
                            "tags": meta["tags"], "categoryId": meta["categoryId"]},
                "status": status}
        media = MediaFileUpload(str(folder / "video.mp4"), mimetype="video/mp4", resumable=True)
        res = yt.videos().insert(part="snippet,status", body=body, media_body=media).execute()
        info = {"id": res["id"], "url": f"https://youtube.com/shorts/{res['id']}",
                "publishAt": status.get("publishAt"), "privacy": res["status"]["privacyStatus"],
                "uploaded": datetime.datetime.now().isoformat(), "sha256": sha256(folder / "video.mp4")}
        info["commentId"] = post_comment(yt, res["id"])
        (folder / "uploaded.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(folder.name, "->", info["url"], info["publishAt"] or "public now")
        if slot:
            slot += datetime.timedelta(days=1)


if __name__ == "__main__":
    main()
