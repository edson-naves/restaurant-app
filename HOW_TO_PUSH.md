# How to push my changes (cheat-sheet)

"Pushing" = sending your saved changes to GitHub. Render then **auto-deploys**
the live site a few minutes later.

```
edit files  →  git add -A  →  git commit -m "..."  →  git push  →  Render deploys
```

---

## ONE-TIME setup (do this once, so pushing never asks for a password again)

Pushing needs to prove you're you. GitHub no longer accepts your account
password — the clean fix is the GitHub CLI, which logs you in via the browser.

In the VS Code terminal (open with **Ctrl + `**):

```sh
winget install --id GitHub.cli      # install the GitHub CLI
```
Close and reopen the terminal, then:
```sh
gh auth login
```
Answer: **GitHub.com** → **HTTPS** → **Yes** → **Login with a web browser** →
copy the one-time code → browser opens → paste code → **Authorize**.

Then tidy up the old embedded token:
```sh
git remote set-url origin https://github.com/edson-naves/restaurant-app.git
```
…and delete the old token at https://github.com/settings/tokens.

That's it — you never deal with tokens/passwords again.

---

## EVERY TIME you want to ship a change

Always start in the project folder:
```sh
cd C:\Users\enave\Projetos\Teste\restaurant_app
```

Then the cycle:
```sh
git status                          # (optional) what did I change?
git add -A                          # stage ALL changes
git commit -m "describe the change" # save a snapshot, with a note
git push                            # send to GitHub  ->  Render redeploys
```

- `git status` — read-only, shows changed files. Safe to run anytime.
- `git add -A` — stages everything (`-A` = all). No output = it worked.
- `git commit -m "..."` — saves the snapshot; prints `N files changed`.
- `git push` — uploads it. After the one-time setup above, no popup.

## Check the deploy
GitHub: your commit appears at github.com/edson-naves/restaurant-app.
Render: dashboard → your service → **Events / Logs** shows a new deploy; the live
site updates in ~3–5 minutes.

## Useful extras
```sh
git log --oneline -5                # last 5 snapshots
git diff                            # exact lines I changed (q to quit)
git pull                            # get changes made elsewhere (rarely needed solo)
```

## Golden rules
- A change isn't live until you **push**. Editing/committing alone changes nothing online.
- Write a short, clear commit message ("Fix login", "Add pizza photos") — future-you will thank you.
- Never paste tokens/passwords into chats. The one-time `gh auth login` means you won't have to.
