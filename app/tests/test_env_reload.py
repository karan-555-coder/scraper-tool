"""The server Serper key in app/.env: a key swapped or removed there reaches
the collection worker (a spawned child process) too, not only the API
process - and a real environment variable still wins. Runs on a copy of
backend/ with its own .env, so the real app/.env is never touched.
Run:  python tests/test_env_reload.py  (from the app/ directory)"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from _scratch import scratch_dir  # noqa: E402  (tests/_scratch.py)

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r'''
import multiprocessing as mp, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ENV = os.path.join(HERE, ".env")
n = [0]
def write_env(key):
    with open(ENV, "w", encoding="utf-8") as f:
        f.write(f"SERPER_API_KEY={key}\n" if key else "# no key\n")
    n[0] += 1                                  # a distinct mtime per edit
    t = time.time() + 10 * n[0]                # (time.time() ticks ~15 ms on Windows)
    os.utime(ENV, (t, t))
def worker(q_in, q_out):                       # started BEFORE the edits, like the real one
    from backend import config
    while q_in.get() != "stop":
        q_out.put(config.serper_api_key())
if __name__ == "__main__":
    os.environ.pop("SERPER_API_KEY", None); os.environ.pop("BDC_SERPER_KEY_FROM_DOTENV", None)
    write_env("OLDKEYOLDKEYOLDKEY01")
    from backend import config
    ctx = mp.get_context("spawn"); q_in, q_out = ctx.Queue(), ctx.Queue()
    w = ctx.Process(target=worker, args=(q_in, q_out)); w.start()
    def both():
        q_in.put("ask"); return config.serper_api_key(), q_out.get(timeout=60)
    assert both() == ("OLDKEYOLDKEYOLDKEY01",) * 2
    write_env("NEWKEYNEWKEYNEWKEY02"); assert both() == ("NEWKEYNEWKEYNEWKEY02",) * 2, both()
    write_env(None);                   assert both() == ("", ""), both()
    write_env("BACKKEYBACKKEYBACK03"); assert both() == ("BACKKEYBACKKEYBACK03",) * 2, both()
    q_in.put("stop"); w.join()
    os.environ.pop("BDC_SERPER_KEY_FROM_DOTENV", None)        # a real env var wins
    os.environ["SERPER_API_KEY"] = "REALENVREALENVREAL04"
    config._DOTENV_KEYS.discard("SERPER_API_KEY")
    write_env("FILEKEYFILEKEYFILE05"); assert config.serper_api_key() == "REALENVREALENVREAL04"
    print("OK")
'''


def main():
    root = scratch_dir(prefix="envreload_test_")
    shutil.copytree(os.path.join(APP, "backend"), os.path.join(root, "backend"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(root, "probe.py"), "w", encoding="utf-8") as f:
        f.write(PROBE)
    env = dict(os.environ, APP_OUTPUT_DIR=os.path.join(root, "output"))
    env.pop("SERPER_API_KEY", None)
    env.pop("BDC_SERPER_KEY_FROM_DOTENV", None)
    r = subprocess.run([sys.executable, "probe.py"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), (r.stdout[-800:], r.stderr[-1500:])
    print("  key swapped in .env -> API and running worker both use it")
    print("  key removed from .env -> no key in either; re-added -> back")
    print("  a real SERPER_API_KEY environment variable still wins")
    print("ALL ENV RELOAD TESTS PASSED")


if __name__ == "__main__":
    main()
