"""Create synthetic participants and stage rosters only in the caller's temporary event."""
from http.client import HTTPConnection
import json
import secrets
from urllib.parse import urlsplit


def participants(service):
    origin = urlsplit(service.origin)
    admin = (service.data_dir / "admin-access.txt").read_text().splitlines()[1]
    session = {}

    def api(path, body=None):
        headers = {"Content-Type": "application/json", "Origin": service.origin, **session}
        connection = HTTPConnection(origin.hostname, origin.port, timeout=10)
        try:
            connection.request("GET" if body is None else "POST", service.base_path + "/api/" + path,
                                    None if body is None else json.dumps(body).encode(), headers)
            response = connection.getresponse()
            assert response.status == 200, f"fixture setup failed: {path} ({response.status})"
            result = json.loads(response.read())
            if path == "login":
                session.update({"Cookie": response.getheader("Set-Cookie").split(";")[0],
                                "X-GOTC-Login": result["loginId"]})
            return result
        finally:
            connection.close()

    api("login", {"role": "admin", "code": admin})
    with service.db() as db:
        db.execute("BEGIN IMMEDIATE")
        profile = service.registration.clean({
            "name": "Browser Fixture Team", "captain": "Fixture Captain", "school": "Fixture School",
            "members": ["Fixture One", "Fixture Two"], "targetSize": 3, "project": "Browser Fixture",
            "phone": "", "summary": "Generated for browser tests",
        })
        team_id = service.registration.insert(db, profile, None)["team_id"]
    team = api("admin/team-access", {"action": "issue", "teamId": team_id})
    judge = api("admin/invitations", {"role": "judge", "name": "Browser Fixture Judge"})
    audience = api("admin/invitations", {"role": "audience", "name": "Browser Fixture Audience"})
    for stage in ["exhibition", "roadshow"]:
        path = "admin/judging/" + stage
        revision = api(path)["stage"]["revision"]
        api(path + "/roster", {"expectedRevision": revision, "teamIds": [team_id], "judgeIds": [judge["id"]]})
    roadshow = api("admin/roadshow-access")
    api("admin/roadshow-access", {"expectedRevision": roadshow["revision"], "runId": roadshow["runId"],
                                  "names": ["Fixture Roadshow Judge"]})
    revision = api("admin")["config"]["writeRevisions"]["voting"]
    api("admin/settings", {"votingOpen": True, "expectedRevisions": {"voting": revision}})
    finals_code = f"{secrets.randbelow(10**12):012d}"
    (service.data_dir / "finals-access-code.txt").write_text(finals_code)
    revision = api("admin/finals")["config"]["revision"]
    seats = api("admin/finals/judges", {"expectedRevision": revision})
    roster = api("admin/finals/roster", {"expectedRevision": seats["config"]["revision"],
                 "teamIds": [team_id], "judgeIds": [s["id"] for s in seats["judges"]]})
    api("admin/finals/settings", {"expectedRevision": roster["config"]["revision"], "open": True, "published": False})
    return {"team": team["code"], "admin": admin, "judge": judge["code"], "audience": audience["code"],
            "finals": finals_code, "seat": seats["judges"][0]["id"], "teamId": team_id}


def activate_organizer(service):
    from pyotp import TOTP
    from unittest.mock import patch
    from backend.gotc.admin_auth import activate, prepare_enrollment

    organizer = prepare_enrollment("browser-organizer", "Browser Fixture Organizer")
    # Model an enrollment completed a minute before this real browser login.
    enrolled_ms = service.clock() - 60000
    with patch.object(service, "clock", return_value=enrolled_ms):
        activate(service, organizer, TOTP(organizer["seed"]).at(enrolled_ms // 1000))
    return organizer
