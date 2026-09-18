# Git Version Control & Development Workflow

This document defines the version control rules and Git synchronization routines between the **Raspberry Pi 5 (Edge DSP Backend)**, the **Host PC (Frontend GUI & Docs)**, and the **GitHub Central Repository**.

---

## 1. Environment & Architecture

```text
                     ┌──────────────────────────┐
                     │      GitHub Remote       │
                     │  [https://github.com/](https://github.com/)...  │
                     │      (origin/main)       │
                     └─────────────┬────────────┘
                                   │
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
   ┌──────────────────────────┐          ┌──────────────────────────┐
   │     Host PC (Local)      │          │   Raspberry Pi 5 (SSH)   │
   ├──────────────────────────┤          ├──────────────────────────┤
   │ • docs/ (DevLog/Pages)   │          │ • src/ (C++ FX Engine)   │
   │ • viewer/viewer.py       │          │ • tests/ (HW diagnostics)│
   │ • VS Code Local Editor   │          │ • CMakeLists.txt         │
   └──────────────────────────┘          └──────────────────────────┘
```

*   **Host PC:** Writes documentation (`docs/`), runs the real-time Python visualizer (`viewer/viewer.py`), and reviews commits.
*   **Raspberry Pi 5:** Implements C++ DSP engines, runs driver callbacks, and executes hardware test binaries (`tests/`).
*   **GitHub (`origin/main`):** Central coordination anchor. Code is never transferred directly via raw copy-paste between devices.

---

## 2. Initial Setup (Run Once Per Device)

### Identity Configuration
Run on both the Pi 5 and the Host PC:
```bash
git config --global user.name "YourGitHubUsername"
git config --global user.email "your_email@example.com"
```

### Essential `.gitignore`
Ensure `.gitignore` exists at the repository root to exclude build artifacts, binary logs, and editor caches:
```gitignore
build/
bin/
*.o
*.so
*.a
*.dat
*.h5
*.fits
solar_data/
logs/
.vscode/
.DS_Store
Thumbs.db
```

---

## 3. Core Development Rules

1.  **Pull First:** Always run `git pull origin main` before touching any code on either device.
2.  **Push on Exit:** Always commit and push your changes before switching machines.
3.  **Inspect State:** Run `git status` before and after staging files to verify what is being included.

---

## 4. Daily Execution Workflows

### Scenario A: Developing on Raspberry Pi 5 (C++ Backend)

Run within the remote SSH terminal on the Pi 5 (`~/solar_correlator`):

```bash
# 1. Pull the latest commits pushed from PC
git pull origin main

# 2. Modify files in src/, tests/, or CMakeLists.txt
# Compile and run verification checks
cmake --build build -j4
./build/alignment_test

# 3. Inspect modified workspace
git status

# 4. Stage and commit changes
# Option 1: Existing tracked files only
git commit -am "fix: correct sample lag offset calculation in time_alignment.cpp"

# Option 2: Newly created files included
git add .
git commit -m "feat: implement lock-free queue in ring_buffer.hpp"

# 5. Push upstream to GitHub
git push origin main
```

---

### Scenario B: Developing on Host PC (Python GUI & Documentation)

Run inside Git Bash or terminal on the Host PC:

```bash
# 1. Fetch latest backend updates from the Pi 5
git pull origin main

# 2. Edit viewer/viewer.py or documentation in docs/
# Preview markdown changes locally

# 3. Check modified files
git status

# 4. Stage, commit, and push
git add docs/ viewer/
git commit -m "docs: document module 2 sample alignment and PNR threshold results"

# 5. Push to GitHub (triggers GitHub Pages automated build)
git push origin main
```

---

## 5. Command Reference

| Command | Description | Notes |
| :--- | :--- | :--- |
| `git status` | Show working tree status | Identifies staged, unstaged, and untracked files. |
| `git pull origin main` | Fetch & merge changes | Run at the beginning of every session. |
| `git add <path>` | Stage specific file or folder | Prepares changes for commit. |
| `git add .` | Stage all changes | Includes edits, deletions, and new files. |
| `git commit -m "..."` | Commit staged files | Creates snapshot with descriptive message. |
| `git commit -am "..."` | Auto-stage & commit | **Tracked files only.** Ignores untracked/new files. |
| `git push origin main` | Upload commits to GitHub | Run before switching to the other device. |
| `git log --oneline -n 5`| Compact commit history | Displays the last 5 commits. |
| `git diff` | View unstaged changes | Shows line-by-line differences before `git add`. |

---

## 6. Troubleshooting & Recovery

### Issue 1: `[rejected - non-fast-forward]` (Forgot to Push on Machine A)
You committed on Machine A but forgot to push. Then you made commits on Machine B and pushed. When pushing from Machine A, Git rejects the upload.

**Fix:**
```bash
# Pull remote commits and rebase your local work on top
git pull --rebase origin main

# Push the rebased commits cleanly
git push origin main
```

---

### Issue 2: "Your branch is ahead of 'origin/main' by N commits"
Local commits were recorded, but they have not been uploaded to GitHub.

**Fix:**
```bash
git push origin main
```

---

### Issue 3: Newly Created File Ignored by `git commit -am`
The `-a` flag only stages files that Git is already tracking. Newly created files are untracked.

**Fix:**
```bash
git add path/to/new_file.cpp
git commit -m "feat: add new module file"
git push origin main
```

---

### Issue 4: Accidentally Staged Binary or Test Files
You ran `git add .` and caught large test logs (`.dat`) or build artifacts (`build/`).

**Fix:**
```bash
# Unstage everything without losing code modifications
git reset

# Verify .gitignore covers the files, then stage clean files
git add src/ tests/ docs/ viewer/
```

---

## 7. Commit Message Convention

Maintain clear, structured commit prefixes for clean project tracking:

*   `feat:` New feature or processing engine (e.g., `feat: add Welch accumulation to FX correlator`).
*   `fix:` Bug fix or calibration tweak (e.g., `fix: invert buffer drain lag index in alignment loop`).
*   `docs:` Documentation or DevLog updates (e.g., `docs: record module 2 zero-baseline test notes`).
*   `test:` Hardware test scripts or harnesses (e.g., `test: add 60-second stability check to alignment_test`).
*   `refactor:` Structural code adjustments without logic changes (e.g., `refactor: extract constants into config.hpp`).