"""Per-user Serper API keys: each user saves their own key, sees it only
masked, never sees another user's, and their searches carry their own key.
Driven through the real ASGI app with in-memory users and a temp key file
(no network: Serper's key check is stubbed).
Run:  python tests/test_keys.py  (from the app/ directory)"""

from __future__ import annotations

import os
import sys
from _scratch import scratch_dir  # noqa: E402  (tests/_scratch.py)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPABASE_DB_URL"] = ""
os.environ["SERPER_API_KEY"] = ""

from backend import auth, keys, main  # noqa: E402
from backend.collector import search  # noqa: E402
from backend.collector.jobs import JobManager  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_auth import call  # noqa: E402

KEY_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaa1111"
KEY_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbb2222"


def signup(email: str) -> str:
    st, data, _ = call("POST", "/api/auth/signup",
                       {"name": email.split("@")[0], "email": email,
                        "password": "long enough pw"}, ip="10.1.0.1")
    assert st == 201, (st, data)
    return data["token"]


def key_of(token: str) -> dict:
    st, data, _ = call("GET", "/api/config", token=token)
    assert st == 200, (st, data)
    return data["serper_key"]


def main_():
    tmp = scratch_dir(prefix="keys_test_")
    auth.use_store(auth.MemoryStore())
    keys.use_store(keys.FileStore(os.path.join(tmp, "user_keys.json")))
    checked = []
    keys.check_with_serper = lambda k: checked.append(k) or 2500
    main.hub.ready.set()        # no collection worker here: serve from memory

    async def no_categories(client):
        return {}
    main.my_categories = no_categories

    ta, tb = signup("a@example.com"), signup("b@example.com")
    assert key_of(ta) == {"source": "", "masked": "", "server_key_available": False}
    print("  no key: collections can't start")

    # bad keys are refused before Serper is even asked
    for bad in ("", "short", "has spaces in it 1234567", "x" * 300):
        st, _, _ = call("POST", "/api/serper-key", {"key": bad}, token=ta)
        assert st in (422,), (bad, st)
    assert not checked
    print("  malformed keys refused")

    st, data, _ = call("POST", "/api/serper-key", {"key": KEY_A}, token=ta)
    assert st == 200 and data["source"] == "user" and data["balance"] == 2500, data
    assert data["masked"].endswith("1111") and KEY_A not in str(data)
    assert checked == [KEY_A]
    print("  key saved after Serper check; reply shows it masked only")

    # A sees only the masked key; B sees nothing of A's
    ka, kb = key_of(ta), key_of(tb)
    assert ka["source"] == "user" and ka["masked"].endswith("1111"), ka
    assert kb["source"] == "" and kb["masked"] == "", kb
    for path in ("/api/config", "/api/status"):
        for tok in (ta, tb):
            _, body, _ = call("GET", path, token=tok)
            assert KEY_A not in str(body), path
    print("  other users never see the key; it never appears in any reply")

    call("POST", "/api/serper-key", {"key": KEY_B}, token=tb)
    assert key_of(ta)["masked"].endswith("1111")
    assert key_of(tb)["masked"].endswith("2222")
    assert keys.user_key(auth.identity(_uid(ta))) == KEY_A
    print("  each user keeps their own key")

    # the server's (demo) key is the fallback for users without one
    tc = signup("c@example.com")
    os.environ["SERPER_API_KEY"] = "serverkeyserverkey00"
    kc = key_of(tc)
    assert kc["source"] == "server" and kc["masked"] == "", kc
    assert key_of(ta)["source"] == "user"
    os.environ["SERPER_API_KEY"] = ""
    print("  users without a key fall back to the server key (never shown)")

    # credits display: balance of the key in use + what the user spent
    who_a = auth.identity(_uid(ta))

    async def fake_call(cmd, timeout=30.0, **args):
        assert cmd == "credits" and args["client"] == who_a
        return {"ledger": {"total_credits": 37}}
    real_call, main.hub.call = main.hub.call, fake_call
    st, cr, _ = call("GET", "/api/serper-key/credits", token=ta)
    main.hub.call = real_call
    assert st == 200 and cr["balance"] == 2500 and cr["used"] == 37, cr
    assert cr["source"] == "user" and cr["masked"].endswith("1111") and not cr["low"], cr
    assert KEY_A not in str(cr)
    keys.check_with_serper = lambda k: 40            # nearly used up
    keys.forget_balance(KEY_A)
    st, cr, _ = call("GET", "/api/serper-key/credits", token=ta)
    assert cr["balance"] == 40 and cr["low"] is True, cr
    keys.check_with_serper = lambda k: 99999         # cached 30 s: Serper not asked again
    st, cr, _ = call("GET", "/api/serper-key/credits", token=ta)
    assert cr["balance"] == 40, cr
    keys.check_with_serper = lambda k: checked.append(k) or 2500
    keys.forget_balance(KEY_A)
    print("  credits left / used shown; low-credit warning; balance cached")

    st, data, _ = call("POST", "/api/serper-key/delete", token=ta)
    assert st == 200 and data["source"] == "", data
    assert key_of(tb)["masked"].endswith("2222")
    print("  remove deletes only the caller's own key")

    # without a session the endpoints are behind the login wall
    st, _, _ = call("POST", "/api/serper-key", {"key": KEY_A})
    assert st == 401
    print("  login required")

    # searches carry their own job's key, even on one shared session
    sent = []

    class Sess:
        headers: dict = {}

        def post(self, url, json=None, timeout=None, headers=None):
            sent.append(headers["X-API-KEY"])
            raise RuntimeError("stop")

    acc = search.AccountLimits()
    acc.session = Sess()
    pa = search.SerperProvider(KEY_A, account=acc, follow_env=False)
    pb = search.SerperProvider(KEY_B, account=acc, follow_env=False)
    for p in (pa, pb, pa):
        try:
            p._send(p.SEARCH_URL, {"q": "x"})
        except RuntimeError:
            pass
    assert sent == [KEY_A, KEY_B, KEY_A], sent
    assert "X-API-KEY" not in Sess.headers
    assert search.account_limits(KEY_A) is not search.account_limits(KEY_B)
    assert search.account_limits(KEY_A) is search.account_limits(KEY_A)
    print("  each request carries its own job's key; separate limits per account")

    # an own-key job: key set on the job, not counted against the IP cap
    class FakeJob:
        def __init__(self, state, category, *a, **kw):
            self.id = f"j{len(made)}"
            self.category, self.status = category, "queued"
            self.credit_budget = 50
            self.crawl_workers = 0
            made.append(self)

        def say(self, *_):
            pass

        def start(self):
            self.status = "running"

    made: list = []
    m = JobManager(max_active=4, job_factory=FakeJob, state_for=lambda c: object())
    j = m.submit("u1", "Finance", [], "", 10, "serper", 5, ip="1.2.3.4",
                 serper_key=KEY_A)
    assert j.serper_key == KEY_A and j.credit_budget == 50
    j2 = m.submit("u2", "Law_Firms", [], "", 10, "serper", 5, ip="1.2.3.5")
    assert j2.serper_key == ""
    print("  jobs carry their owner's key; the server key's IP cap skips own keys")

    # the app's key runs dry mid-collection: the user adds their own key and
    # the paused job continues on it (balance checked at once, not in 60 s)
    import threading
    import time
    from backend.collector.engine import CollectionJob
    job = CollectionJob.__new__(CollectionJob)
    job.lock, job.serper_key, job.status = threading.Lock(), "", "running"
    job._credit_wait_since, job._credit_ckpt_done, job._recovery_kind = 0.0, True, ""
    job.recovery, job.target, lines = "", 100, []
    job.say = lines.append
    job.category_count = lambda: 40
    job.checkpoint = lambda force=False: None
    acc0 = search.account_limits("serverkeyserverkey00")
    prov = search.SerperProvider("serverkeyserverkey00", account=acc0)
    balances = {"serverkeyserverkey00": 0, KEY_A: 900}
    prov.balance = lambda: balances[prov.api_key]
    prov.reenable = lambda: None
    job.provider = prov
    job._enter_credit_wait("out of credits")
    assert job.status == "waiting" and "own Serper API key" in job.recovery, job.recovery
    job._check_credits()
    assert job.status == "waiting"            # app key still empty: keeps waiting
    job.use_serper_key(KEY_A)
    assert prov.api_key == KEY_A and prov.follow_env is False and job.serper_key == KEY_A
    assert prov.rate is search.account_limits(KEY_A).rate
    assert job._next_credit_check <= time.time()
    job._check_credits()
    assert job.status == "running", job.status
    assert any("Resumed" in x for x in lines), lines
    job.use_serper_key("")                    # key removed: back to the app's key
    assert prov.follow_env is True and job.serper_key == ""
    print("  app credits used up -> user's own key -> paused search resumes at once")

    # ledger: credits split by key (shared server key vs the user's own)
    from backend.collector.credits import CreditLedger
    led = CreditLedger()
    led.record("k1", kind="places", query="q in x", page=1, category="Finance",
               credits=3, results=5, new=0)
    led.record("k2", kind="organic", query="q2 in x", page=1, category="Finance",
               credits=2, results=5, new=0, key_source="own")
    led.record("k3", kind="organic", query="q3 in x", page=1, category="Finance",
               credits=0, results=5, new=0, source="cache")
    s1 = led.summary(top=0)
    assert (s1["total_credits"], s1["server_key_credits"], s1["own_key_credits"]) == (5, 3, 2), s1
    assert s1["last_used"] > 0
    s2 = CreditLedger(led.to_dict()).summary(top=0)          # survives a save
    assert (s2["server_key_credits"], s2["own_key_credits"]) == (3, 2)
    old = led.to_dict(); old.pop("by_key"); old.pop("last_used")
    s3 = CreditLedger(old).summary(top=0)                     # older saves: all shared key
    assert (s3["server_key_credits"], s3["own_key_credits"]) == (5, 0) and s3["last_used"] > 0
    print("  ledger splits credits: shared key vs own key (old saves = shared key)")

    # admin usage report: admins only, every account listed with its usage
    auth.use_store(auth.MemoryStore())
    tadm, tusr = signup("boss@example.com"), signup("worker@example.com")
    os.environ["ADMIN_EMAILS"] = "Boss@Example.com, other@example.com"
    try:
        st, me, _ = call("GET", "/api/auth/me", token=tadm)
        assert me["user"]["is_admin"] is True, me
        st, me, _ = call("GET", "/api/auth/me", token=tusr)
        assert me["user"]["is_admin"] is False, me
        st, _, _ = call("GET", "/api/admin/usage", token=tusr)
        assert st == 403, st
        st, _, _ = call("GET", "/api/admin/usage")
        assert st == 401, st
        wid = auth.identity(_uid(tusr))

        async def fake_usage(cmd, timeout=30.0, **args):
            assert cmd == "usage" and wid in args["owners"] and len(args["owners"]) == 2
            return {o: ({"credits": 12, "server_key_credits": 10, "own_key_credits": 2,
                         "searches": 7, "records": 40, "last_used": 1.0, "running": False}
                        if o == wid else {"credits": 0, "server_key_credits": 0,
                                          "own_key_credits": 0, "searches": 0, "records": 0,
                                          "last_used": 0, "running": False})
                    for o in args["owners"]}
        real_call, main.hub.call = main.hub.call, fake_usage
        try:
            st, rep, _ = call("GET", "/api/admin/usage", token=tadm)
        finally:
            main.hub.call = real_call
        assert st == 200, (st, rep)
        assert [u["email"] for u in rep["users"]] == ["worker@example.com", "boss@example.com"], rep
        top = rep["users"][0]
        assert top["credits"] == 12 and top["own_key_credits"] == 2 and top["id"] == _uid(tusr)
        assert rep["users"][1]["is_admin"] is True
        assert rep["totals"]["credits"] == 12 and rep["totals"]["records"] == 40
        assert "password" not in str(rep) and "scrypt" not in str(rep)
    finally:
        os.environ["ADMIN_EMAILS"] = ""
    print("  admin usage report: admins only; every account with its credits")

    # admin login activity: online now, sessions, logins / logouts, history
    auth.use_store(auth.MemoryStore())
    auth.last_seen.clear()
    os.environ["ADMIN_EMAILS"] = "boss2@example.com"
    try:
        tb = signup("boss2@example.com")
        tw = signup("worker2@example.com")
        call("POST", "/api/auth/login", {"email": "worker2@example.com",
                                         "password": "wrong password"}, ip="10.7.0.9")
        st, d, _ = call("POST", "/api/auth/login", {"email": "worker2@example.com",
                                                    "password": "long enough pw"}, ip="10.7.0.8")
        tw2 = d["token"]
        call("GET", "/api/config", token=tw2)              # worker2 is active now
        st, _, _ = call("POST", "/api/auth/logout", token=tw, ip="10.7.0.7")
        assert st == 200
        st, _, _ = call("GET", "/api/admin/activity", token=tw2)
        assert st == 403, st
        st, act, _ = call("GET", "/api/admin/activity", token=tb)
        assert st == 200, (st, act)
        sm = act["summary"]
        assert (sm["signups_today"], sm["logins_today"], sm["logouts_today"],
                sm["failed_today"]) == (2, 1, 1, 1), sm
        assert sm["users"] == 2 and sm["logged_in_users"] == 2 and sm["active_sessions"] == 2, sm
        assert sm["online_now"] == 2, sm                   # both made requests just now
        w = next(u for u in act["users"] if u["email"] == "worker2@example.com")
        assert w["sessions"] == 1 and w["last_login"] and w["last_logout"] and w["online"], w
        call("POST", "/api/auth/logout", token=tw2, ip="10.7.0.6")
        st, act2, _ = call("GET", "/api/admin/activity", token=tb)
        w = next(u for u in act2["users"] if u["email"] == "worker2@example.com")
        assert not w["online"] and w["sessions"] == 0, w   # logged out = offline at once
        kinds = [e["kind"] for e in act["events"]]
        assert kinds[0] == "logout" and kinds.count("signup") == 2 and "login_failed" in kinds, kinds
        failed = next(e for e in act["events"] if e["kind"] == "login_failed")
        assert failed["email"] == "worker2@example.com" and failed["ip"] == "10.7.0.9", failed
        assert "password" not in str(act).replace("login_failed", "") and "token" not in str(act)
    finally:
        os.environ["ADMIN_EMAILS"] = ""
    print("  admin login activity: online, sessions, logins/logouts/failed, history")

    # local accounts (APP_LOCAL_ACCOUNTS=1) survive a restart
    path = os.path.join(tmp, "accounts.json")
    auth.use_store(auth.FileStore(path))
    tok = signup("keep@example.com")
    auth.use_store(auth.FileStore(path))           # "restart": reload from disk
    st, data, _ = call("POST", "/api/auth/login",
                       {"email": "keep@example.com", "password": "long enough pw"},
                       ip="10.1.0.2")
    assert st == 200, (st, data)
    st, _, _ = call("GET", "/api/auth/me", token=tok)
    assert st == 200
    raw = open(path, encoding="utf-8").read()
    assert "long enough pw" not in raw and tok not in raw
    print("  local accounts + sessions survive a restart (no password/token in clear)")

    # an unreadable / corrupt accounts file is never replaced by an empty one
    good = open(path, encoding="utf-8").read()
    for broken in (good[:-5], "[]", '{"users": []}'):
        with open(path, "w", encoding="utf-8") as f:
            f.write(broken)
        auth.use_store(auth.FileStore(path))
        st, data, _ = call("POST", "/api/auth/signup",
                           {"name": "x", "email": "new@example.com",
                            "password": "long enough pw"}, ip="10.1.0.3")
        assert st == 503, (broken[:20], st, data)
        st, _, _ = call("GET", "/api/config", token=tok)
        assert st == 503, st
        assert open(path, encoding="utf-8").read() == broken   # left untouched
    with open(path, "w", encoding="utf-8") as f:      # repaired: everyone is back
        f.write(good)
    st, data, _ = call("POST", "/api/auth/login",
                       {"email": "keep@example.com", "password": "long enough pw"},
                       ip="10.1.0.4")
    assert st == 200, (st, data)
    assert os.path.exists(path + ".bak")
    print("  corrupt accounts file: 503 + file untouched (no wipe); works once repaired")

    # with an access code, the code is asked for before the login
    os.environ["APP_AUTH_TOKEN"] = "office-code-123"
    try:
        st, data, hdrs = call("GET", "/api/config")
        assert st == 401 and data["detail"] == "Access code required", data
        assert "x-login-required" not in hdrs
        st, data, _ = call("POST", "/api/auth/login",
                           {"email": "keep@example.com", "password": "long enough pw"},
                           ip="10.1.0.5")
        assert st == 401 and data["detail"] == "Access code required", data
    finally:
        os.environ["APP_AUTH_TOKEN"] = ""
    print("  access code is checked before the login wall")

    # /api/health says why the user database can't be used - never the password
    from backend import config as cfg, db
    real = cfg.DATABASE_URL
    try:
        for url, expect in (
                ("", "off"),
                ("postgresql://postgres.x:[YOUR-PASSWORD]@h.example:5432/postgres", "placeholder"),
                ("postgresql://postgres.x:S3cretPw9@no-such-host.invalid:5432/postgres", "host not found")):
            cfg.DATABASE_URL = url
            db._status.update(t=0.0, value="")
            st, health, _ = call("GET", "/api/health")
            assert st == 200 and expect in health["database"], (url, health)
            assert "S3cretPw9" not in str(health)
    finally:
        cfg.DATABASE_URL = real
        db._status.update(t=0.0, value="")
    print("  /api/health reports why the database is unavailable (no password)")

    print("ALL KEY TESTS PASSED")


def _uid(token: str) -> str:
    _, data, _ = call("GET", "/api/auth/me", token=token)
    return data["user"]["id"]


if __name__ == "__main__":
    main_()
