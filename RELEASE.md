# Releasing Kemi

Everything for distribution is already built and tested (CI matrix, the PyPI
publish workflow, the one-line installer, the Docker image). Cutting a release
is **three manual steps** — all on your accounts, none of them code.

## One-time setup

1. **Make the repository public** on GitHub (Settings → General → Danger Zone),
   and merge this branch into `main` (the installer and README link to `main`).
2. **Enable PyPI Trusted Publishing** at
   <https://pypi.org/manage/account/publishing/>:
   - PyPI project name: `kemi`
   - Owner / repo: `emred530-blip` / `Kemi`
   - Workflow file: `release.yml`
   - Environment: `pypi`

## Cut a release

3. Tag and push — the `release.yml` workflow runs the tests, builds the
   sdist + wheel, and publishes to PyPI automatically:

   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

That's it. Within a few minutes anyone can:

```bash
pip install kemi          # PyPI
curl -fsSL https://raw.githubusercontent.com/emred530-blip/Kemi/main/scripts/install.sh | sh
docker compose up         # a containerised fleet
```

## Pre-tag checklist (optional, all already green here)

```bash
python3 -m unittest discover -s tests   # full suite
python3 -m build                         # sdist + wheel build clean
python3 -m kemi demo                     # end-to-end smoke
```

Bump `__version__` in `kemi/__init__.py` and `version` in `pyproject.toml`,
add a `CHANGELOG.md` entry, then tag to match.
