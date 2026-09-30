"""A real demo audit driven through the web UI (synthetic data, no network)."""
import time

from fastapi.testclient import TestClient

import jobs
import webapp


def test_demo_job_end_to_end(tmp_path):
    cfg = webapp.WebConfig(data_dir=tmp_path, password="pw")
    m = jobs.JobManager(tmp_path, webapp.AUDIT_PY, poll=0.1)
    with TestClient(webapp.create_app(cfg, m, jobs.Scheduler(m, [], tmp_path / "s.json"))) as c:
        c.auth = ("admin", "pw")
        r = c.post("/jobs", data={"kind": "demo"}, follow_redirects=False)
        assert r.status_code == 303
        jid = r.headers["location"].rsplit("/", 1)[1]
        t0, job = time.time(), {}
        while time.time() - t0 < 300:
            job = c.get(f"/api/jobs/{jid}").json()
            if job["status"] in jobs.TERMINAL:
                break
            time.sleep(0.5)
        assert job["status"] == "done", job["log"][-1500:]
        assert job["reports"] and job["reports"][0].startswith("DEMOUSDT_4h_")
        rep = c.get("/reports/" + job["reports"][0])
        assert rep.status_code == 200 and "Coin audit: DEMOUSDT" in rep.text and "<table>" in rep.text and "Strategy library" in rep.text
        dash = c.get("/dashboard")
        assert dash.status_code == 200 and "DEMOUSDT" in dash.text
        assert (tmp_path / "logs" / "audits.csv").exists() and (tmp_path / "cache" / "demo" / "DEMOUSDT_15m.parquet").exists()
        assert c.get("/").text.count("Demo check") >= 2                            # button + the finished job in the list
