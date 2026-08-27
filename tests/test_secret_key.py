"""SECRET_KEY fail-closed contract.

Every case runs in its own SUBPROCESS. Once ``app.security`` is imported its
module object is cached, and the import-time validation will not run again — so
exercising several environments inside one process would test the first one
eleven times and report success. The subprocess also isolates the environment
itself: each case gets a dict built from scratch rather than a mutated
``os.environ`` that leaks into the next case.

This module deliberately does NOT import ``tests/_env.py``. That module declares
the opt-out, which is one of the variables under test here; importing it would
make the "no opt-out" half of the matrix unreachable.

Run:
    python tests/test_secret_key.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ok = True


def check(cond, label, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  -> ' + str(detail)) if detail else ''}")


def boot(env_overrides: dict[str, str | None], probe: str = ""):
    """Import app.security in a clean subprocess; return (rc, stderr, stdout).

    The child environment starts from a copy of ours with BOTH variables under
    test removed, so a value inherited from the developer's shell can never make
    a case pass or fail by accident.
    """
    env = dict(os.environ)
    env.pop("SECRET_KEY", None)
    env.pop("ALLOW_INSECURE_DEV_SECRET", None)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    code = "import sys; sys.path.insert(0, r'%s')\nimport app.security as s\n%s" % (REPO, probe)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, cwd=str(REPO), timeout=120)
    return p.returncode, (p.stderr or ""), (p.stdout or "")


def refuses(env, label):
    rc, err, _ = boot(env)
    leaked = "aaaa" in err or "dev-insecure-secret" in err.split("public development key")[-1]
    check(rc != 0 and "InsecureSecretKey" in err, label, f"rc={rc}")
    check(not leaked, f"   ^ a mensagem não vaza o segredo", "ok")


def boots(env, label, expect_dev_secret=None):
    probe = ("print('USES_DEV' if s._secret() == s._DEV_SECRET.encode() else 'USES_REAL')")
    rc, err, out = boot(env, probe)
    check(rc == 0, label, f"rc={rc} {err.strip().splitlines()[-1:] or ''}")
    if expect_dev_secret is not None and rc == 0:
        want = "USES_DEV" if expect_dev_secret else "USES_REAL"
        check(want in out, f"   ^ usa {'a chave pública' if expect_dev_secret else 'o segredo real'}",
              out.strip())


GOOD = "x" * 32                      # exatamente o mínimo, ASCII
LONG = "s3cr3t-" + "y" * 60
SHORT31 = "a" * 31                   # 31 caracteres ASCII = 31 bytes
ACCENT16 = "é" * 16                  # 16 caracteres, 32 BYTES em UTF-8

print("=== SECRET_KEY: contrato fail-closed ===\n")
print("-- sem opt-out --")
refuses({}, "1. ambiente limpo, segredo ausente -> recusa o boot")
refuses({"SECRET_KEY": None}, "2. SECRET_KEY ausente -> recusa")
boots({"SECRET_KEY": LONG}, "3. segredo válido -> sobe", expect_dev_secret=False)

print("\n-- opt-out explícito --")
boots({"ALLOW_INSECURE_DEV_SECRET": "1"},
      "4. opt-out + segredo ausente -> sobe com a chave pública", expect_dev_secret=True)
boots({"ALLOW_INSECURE_DEV_SECRET": "1", "SECRET_KEY": LONG},
      "5. opt-out + segredo válido -> usa o segredo real", expect_dev_secret=False)

print("\n-- opt-out NÃO é passe livre: valor presente porém inválido --")
refuses({"ALLOW_INSECURE_DEV_SECRET": "1", "SECRET_KEY": ""},
        "6. opt-out + segredo vazio -> recusa")
refuses({"ALLOW_INSECURE_DEV_SECRET": "1", "SECRET_KEY": "   "},
        "7. opt-out + só espaços -> recusa")
refuses({"ALLOW_INSECURE_DEV_SECRET": "1",
         "SECRET_KEY": "dev-insecure-secret-set-SECRET_KEY-in-production"},
        "8. opt-out + a própria chave pública -> recusa")
refuses({"ALLOW_INSECURE_DEV_SECRET": "1", "SECRET_KEY": SHORT31},
        "9. opt-out + 31 bytes -> recusa")
refuses({"SECRET_KEY": " " + LONG},
        "9b. espaço à esquerda -> recusa; a chave NÃO é normalizada")
refuses({"SECRET_KEY": LONG + "\n"},
        "9c. quebra de linha à direita -> recusa; a chave NÃO é normalizada")

print("\n-- somente o literal \"1\" habilita o opt-out --")
for val in ("0", "true", "yes", "on", "", " 1 ", "01"):
    refuses({"ALLOW_INSECURE_DEV_SECRET": val},
            f"10. opt-out={val!r} + segredo ausente -> recusa")

print("\n-- o limite é em BYTES, não em caracteres --")
boots({"SECRET_KEY": GOOD}, "11. 32 caracteres ASCII = 32 bytes -> sobe", expect_dev_secret=False)
boots({"SECRET_KEY": ACCENT16},
      "12. 16 caracteres 'é' = 32 BYTES -> sobe (metade dos caracteres)",
      expect_dev_secret=False)
refuses({"SECRET_KEY": SHORT31}, "13. 31 caracteres ASCII = 31 bytes -> recusa")

print("\n-- a chave é devolvida VERBATIM, sem normalização --")
_verbatim = ("import os; "
             "print('EXACT' if s._secret() == os.environ['SECRET_KEY'].encode() "
             "else 'ALTERED')")
rc, err, out = boot({"SECRET_KEY": LONG}, _verbatim)
check(rc == 0 and "EXACT" in out,
      "13b. os bytes devolvidos são os do valor original, byte a byte", out.strip())

print("\n-- a falha acontece no IMPORT, não na primeira assinatura --")
rc, err, _ = boot({}, "print('NAO DEVERIA CHEGAR AQUI')")
check(rc != 0 and "NAO DEVERIA" not in err, "14. o módulo nem termina de importar", f"rc={rc}")

print("\n" + ("RESULT: PASS" if ok else "RESULT: FAIL"))
sys.exit(0 if ok else 1)
