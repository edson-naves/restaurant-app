"""TZ policy — the app may only set TZ where tzset can apply it right away.

Every case runs in its own SUBPROCESS. The decision happens once, at
``app.main`` import time, and the MS CRT caches the zone at the first local-time
conversion — so two cases in one process would test the first one twice. Each
child's environment is built from ours with TZ removed, so a value inherited
from the developer's shell cannot make a case pass.

Two things are neutralised in the child before ``app.main`` is imported:

* the ``.env`` loader, via python-dotenv's own ``PYTHON_DOTENV_DISABLED``
  (dotenv/main.py:412 — checked before the path is even resolved). Without it a
  ``.env`` someone adds later (git-ignored, absent today) would feed TZ into
  every case and the matrix would silently measure that file instead of the
  code. Case 6 proves the flag is doing real work.
* ``time.tzset`` — deleting it gives the Windows branch on Unix, installing a
  stub gives the Unix branch on Windows. The stub records that it was called.

Cases 3 and 4 assert the DECISION only (which TZ, tzset applied), never the
child's clock: on Windows the simulated Unix branch really does set TZ, and the
CRT really does mis-parse it — that is the platform, not a regression.

Run:
    python tests/test_timezone.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ok = True


def check(cond, label, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  -> ' + str(detail)) if detail else ''}")


TZSET = {
    True: "time.tzset = lambda: calls.append(1)",
    False: "hasattr(time, 'tzset') and delattr(time, 'tzset')",
    None: "pass",
}


def boot(tzset, tz=None, env_file=None, dotenv_disabled=True):
    """Import app.main in a clean subprocess; return the child's report.

    ``tzset``: True installs a stub, False removes the real one, None leaves the
    platform alone.
    ``dotenv_disabled``: sets PYTHON_DOTENV_DISABLED=1 in the child — the
    library's own switch, and the default for the whole matrix.
    ``env_file``: a path makes app.main's bare ``load_dotenv()`` call the REAL
    loader against that file, which is how case 6 simulates a ``.env`` being
    discovered without writing one into the repository (``find_dotenv()`` would
    look next to app/main.py).

    The child reports the TZ it ended up with, whether tzset ran, and its own
    clock read AFTER the import — which is the -2816 regression.
    """
    env = dict(os.environ)
    env.pop("TZ", None)
    env["APP_ENV"] = "test"
    env["ALLOW_INSECURE_DEV_SECRET"] = "1"
    if dotenv_disabled:
        env["PYTHON_DOTENV_DISABLED"] = "1"
    else:
        env.pop("PYTHON_DOTENV_DISABLED", None)
    if tz is not None:
        env["TZ"] = tz
    redirect = ""
    if env_file is not None:
        redirect = ("import dotenv\n"
                    "_real = dotenv.load_dotenv\n"
                    f"dotenv.load_dotenv = lambda *a, **k: _real(r'{env_file}', override=False)")
    with tempfile.TemporaryDirectory() as tmp:
        # A throwaway database: this suite must not touch the dev one.
        env["DATABASE_URL"] = f"sqlite:///{Path(tmp).as_posix()}/tz.db"
        code = "\n".join([
            "import json, os, sys, time",
            f"sys.path.insert(0, r'{REPO}')",
            "calls = []",
            redirect,
            TZSET[tzset],
            "import app.main  # the decision under test",
            "from datetime import datetime",
            "print(json.dumps({'tz': os.environ.get('TZ'),",
            "                  'tzset_calls': len(calls),",
            "                  'now': datetime.now().isoformat()}))",
        ])
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env, cwd=str(REPO), timeout=180)
    if p.returncode != 0:
        return {"rc": p.returncode, "stderr": p.stderr.strip()[-400:]}
    return json.loads(p.stdout.strip().splitlines()[-1])   # migrate prints first


def skew(report, before):
    return abs((datetime.fromisoformat(report["now"]) - before).total_seconds())


print("=== política de TZ: só onde tzset pode aplicá-la ===\n")

print("-- plataforma sem tzset (Windows, ou simulada) --")
before = datetime.now()
r = boot(tzset=False)
check(r.get("tz") is None, "1. sem tzset: a aplicação não define TZ", r.get("tz"))
check(r.get("tzset_calls") == 0, "1a. sem tzset: nada é aplicado", r.get("tzset_calls"))
check(skew(r, before) < 60,
      "1b. sem tzset: o relógio do processo não desloca  <- regressão -2816",
      f"{skew(r, before):.1f}s")

r = boot(tzset=False, tz="UTC")
check(r.get("tz") == "UTC", "2. sem tzset: TZ do operador é preservado", r.get("tz"))

print("\n-- plataforma com tzset (Unix, ou simulada) --")
r = boot(tzset=True)
check(r.get("tz") == "America/Vancouver", "3. com tzset: assume o fuso do venue", r.get("tz"))
check(r.get("tzset_calls") == 1, "3a. com tzset: aplicado uma vez", r.get("tzset_calls"))

r = boot(tzset=True, tz="Europe/Lisbon")
check(r.get("tz") == "Europe/Lisbon", "4. com tzset: TZ do operador vence o padrão", r.get("tz"))
check(r.get("tzset_calls") == 1, "4a. com tzset: o do operador é aplicado", r.get("tzset_calls"))

print("\n-- plataforma real, sem simulação --")
before = datetime.now()
r = boot(tzset=None)
if hasattr(time, "tzset"):
    check(r.get("tz") == "America/Vancouver",
          "5. POSIX: fuso do venue assumido e aplicado", r.get("tz"))
else:
    check(r.get("tz") is None, "5. Windows: TZ não é definido", r.get("tz"))
    check(skew(r, before) < 60, "5a. Windows: relógio do processo intacto",
          f"{skew(r, before):.1f}s")

print("\n-- hermetismo contra um .env futuro --")
with tempfile.TemporaryDirectory() as tmp:
    envfile = Path(tmp) / ".env"
    envfile.write_text("TZ=America/Vancouver\n", encoding="utf-8")

    # 6. Sem o flag, com o carregador real lendo esse arquivo: a matriz mediria
    #    o .env do operador em vez do código — TZ aparece no ramo sem tzset.
    r = boot(tzset=False, env_file=envfile, dotenv_disabled=False)
    check(r.get("tz") == "America/Vancouver",
          "6. sem o flag: um .env com TZ contaminaria a matriz", r.get("tz"))

    # 6a. MESMO arquivo, MESMO carregador real, só o flag muda. load_dotenv
    #     curto-circuita antes de olhar o caminho (dotenv/main.py:412).
    r = boot(tzset=False, env_file=envfile, dotenv_disabled=True)
    check(r.get("tz") is None,
          "6a. mesmo .env, com PYTHON_DOTENV_DISABLED=1: nada entra", r.get("tz"))

print()
print("RESULT:", "tz policy holds" if ok else "TZ POLICY FAILURES")
sys.exit(0 if ok else 1)
