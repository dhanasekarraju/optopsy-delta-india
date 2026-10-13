# Publish project to your GitHub account

## Why the repository was not created automatically

The available GitHub connector permits reading repositories and writing files in existing repositories, but does not expose repository creation. The project has been committed locally so that its history can be imported without changing upstream Optopsy.

## One-time GitHub action

Visit https://github.com/new and create an **empty**, preferably **private** repository named `optopsy-delta-india` under `dhanasekarraju`. Do not add a generated README, .gitignore, or license during creation: this project already has its own source and initial Git commit.

## Push from WSL using the accompanying Git bundle

1. Download `optopsy-delta-india.bundle` from this chat to your WSL directory (or copy it there from Windows).
2. Run (inside directory containing the `.bundle` file):

```bash
git clone optopsy-delta-india.bundle optopsy-delta-india
cd optopsy-delta-india
git remote remove origin
git remote add origin https://github.com/dhanasekarraju/optopsy-delta-india.git
git push -u origin main
```

GitHub authentication must be provided by you using a personal access token or SSH credentials (do not send tokens to ChatGPT). The bundle contains no private API credentials.

## VPS

Do **not** copy this into a live bot directory. Use a separate path such as `~/optopsy-delta-india` and a separate Python 3.12/3.13 virtual environment. Delta API keys, if used, should initially have read-only permissions. Market-data capture can run with **no key**.
