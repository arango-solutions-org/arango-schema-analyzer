---
name: pr-review
description: Review one pull request on arango-schema-analyzer the way this repository needs — verify every claim the PR makes with a command rather than by reading, apply the repo's own rules (CHANGELOG discipline, PRD traceability, converge-don't-fork with RSA/r2g, byte-identical schema copies, regenerated snapshot goldens, reachable-through-the-contract), and produce a draft review for the user to approve before anything is posted. Use when asked to review a PR, check whether a PR can merge, or re-check a PR after its author pushed fixes. NOT a whole-repo audit.
---

# PR Review

One pull request, reviewed by verification. The built-in `/review` reads a diff
and opines; this runs the repo's own checks against the branch, and the review
it drafts says which claims were executed and which remain read.

## Invocation

`/pr-review 10` — full review, draft only.
`/pr-review 10 recheck` — the author pushed after "changes requested": verify
each requested change against the review's own reproduction, nothing else.
`/pr-review 10 post` — post the approved draft (only after the user has seen it).

## The two rules that make it worth running

**Nothing is posted until the user has read the draft and said so.** Draft in the
reply, offer to post in one line, post verbatim when told. The user decides tone,
timing and whether it goes out at all.

**Verify, don't opine.** Every finding names the command that produced it or says
"read, not executed". The findings that change anything are the ones a command
caught: a test count that doesn't match the description, a cited commit that
doesn't exist, a schema copy that drifted from its twin, a "closes #N" whose
issue has bullets the PR never touches, a fix with no test that fails without it.
Re-run before you post — a mis-escaped digest or a field read from the wrong key
is itself a wrong finding.

## This repo's shape — read before anything

**Dual mirror.** `origin` fetches from **`arango-solutions/arango-schema-analyzer`**
(primary) and every push goes to BOTH that and **`ArthurKeen/arango-schema-analyzer`**
(secondary). `gh` defaults to the arango-solutions repo; pass `--repo` explicitly.

**The same PR number is a DIFFERENT pull request on each mirror.** `#10` is the
naming fix on arango-solutions and a long-merged fingerprint PR on ArthurKeen.
Always resolve which mirror the user means first, and name that repo in every
`gh` call and in the draft. When unsure, check both and say which you reviewed.

**Merges are merge commits, not squashes** (`gh pr merge N --merge`). After a
merge on one mirror, the other is behind — sync it: `git fetch origin && git
checkout main && git merge --ff-only origin/main && git push origin main` (the
dual push-remote carries it to both). The GitHub merge alone only updates the
mirror it ran on.

**Venv shebangs point at a stale path** (the repo was renamed) — invoke tools as
`.venv/bin/python -m pytest` / `-m mypy` / `-m ruff`, never the bare console
scripts.

## Method

### 0. Load the context before reading the diff

```sh
R=arango-solutions/arango-schema-analyzer   # or ArthurKeen/... — confirm which
gh pr view N --repo $R \
  --json title,author,headRefName,baseRefName,headRefOid,isDraft,mergeStateStatus,files,body,reviews,comments
gh pr checks N --repo $R
```

Read **existing reviews first**. If one already requests changes, the job is
usually `recheck`, not a second opinion.

Fetch by **branch name**, never by short SHA (`git fetch origin <sha7>` fails):

```sh
git fetch origin && H=origin/<headRefName>
git log --oneline origin/main..$H
git diff origin/main...$H --stat
```

Then read the whole diff and every new file in full — not the first screen.

### 1. Does it merge, really

GitHub's `mergeStateStatus` lags. Do the merge in a throwaway worktree:

```sh
W=$(mktemp -d)/wt && git worktree add -q --detach "$W" origin/main && cd "$W" \
  && (git merge -q --no-edit "$H" && echo clean || { git diff --name-only --diff-filter=U; git merge --abort; }) \
  ; cd - && git worktree remove --force "$W"
```

### 2. Run what the description says was run

Check out the head in a worktree and run the named tests against the worktree's
source, then compare counts to the description (two different "N passed" figures
in one PR is a finding):

```sh
.venv/bin/python -m pytest <paths the PR names> -q
.venv/bin/python -m pytest -q                          # the full suite
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy schema_analyzer/
```

Integration tests need Docker ArangoDB (`docker compose up -d`, `RUN_INTEGRATION=1`,
port 18529) and are usually left to CI; say "integration read, not executed" unless
you actually ran them.

### 3. Verify every claim that can be verified

| The PR says | Run |
|---|---|
| "fixes a bug" / "regression test" | revert the fix in the worktree, run the new test — it must fail without the fix |
| "closes #N" / "fixes #N" | read the issue; does the PR address each bullet, and say which it does not |
| "converges with / same fix as relational-schema-analyzer X / r2g Y" | open the sibling repo's copy (`../relational-schema-analyzer`, `../r2g`) and diff the logic — converge, don't fork; a fix here usually matches theirs or needs porting |
| "schema change" (tool-contract or CSI) | the two copies must be **byte-identical** — `schema_analyzer/tool_contract/v1/*` vs `docs/tool-contract/v1/*`, `schema_analyzer/csi/v1/*` vs `docs/csi/v1/*`; run `test_tool_contract_schema_parity.py` and `test_csi.py`, and `diff` the files yourself |
| "new request/response field" | is it **reachable through the contract**? `input`/`request` are `additionalProperties: false`, so a field the schema doesn't declare is silently rejected and the feature is unreachable (the `priorRun` lesson — reuse the declared `input.previousAnalysis`, don't mint a sibling key) |
| "snapshot format bump" / new `SNAPSHOT_FORMAT_VERSION` | the golden fixtures are regenerated — run `test_snapshot_golden.py` and `test_snapshot_fingerprints.py` |
| "N passed, M% coverage" | re-run and compare; coverage below the gate is a finding |
| "built by `scripts/foo.py`" / "see `path`" | `git cat-file -e "$H:path"` |

### 4. The repository's own rules

- **CHANGELOG discipline.** Every user-facing change has an entry under
  `## Unreleased` in `CHANGELOG.md`. A feature that merged with an empty
  Unreleased section is a gap (it is how a shipped change went undocumented
  before — the review should catch it, not the next release).
- **PRD traceability** (`docs/PRD.md`, dark-factory `/prd-sync`). Implementation
  traces to a requirement. The PRD is not edited silently inside a feature PR to
  match the code — code-ahead-of-PRD is a proposed patch via `/prd-sync`, never a
  quiet absorption. Evidence for "implemented" must name a `file:line` and pass
  `check_evidence.py`.
- **Converge, don't fork.** The FK-inference engine and the CSI schema are
  copy-ported across arango-schema-analyzer / relational-schema-analyzer / r2g.
  A change to shared semantics should match the siblings or say why it diverges;
  "same fix as RSA 0.9.0 / r2g 0.4.2" is a claim to verify, not take on faith.
  - `fk_inference.py` is a **hand port** of RSA's, with every divergence marked
    `ARANGO:`. For a change there, diff it against RSA's core
    (`../relational-schema-analyzer/relational_schema_analyzer/fk_inference.py`).
    An unmarked difference is a fork. Going the other way, an RSA release that
    changes the core needs a port here. RSA 0.9.0 did not change it; it only
    added a Snowflake sampler and its factory.
  - **One singularizer, two names.** `utils.singularize` (FK inference, type
    detection, quality, eval scoring) delegates to `csi.naming.singularize`
    since r2g-arango#30; the two used to disagree. A naming fix changes
    `csi.naming` and is checked against the vendored
    `csi/naming_known_answers.py`, shared with relational-schema-analyzer and r2g.
- **Schema copies byte-identical.** The in-package schema and its `docs/` twin
  are kept identical and parity-tested; a change to one that misses the other is
  a finding even when every other test passes.
- **Test what you touch.** New behaviour has a test that fails without the fix.
  If the PR claims a regression test, revert the fix in the worktree and run it.
- **Secrets never land.** `.env` holds `PYPI_TOKEN`/`ARANGO_PASSWORD`/LLM keys
  and is gitignored — a committed credential, or a token in a fixture or log, is
  a must-fix. Publishing is outward-facing and irreversible.
- **PROJECT_ID stays `arango-schema-mapper`** in `CLAUDE.md` even though the repo
  renamed — it keys shared-memory state. A PR "fixing" it to match the new name
  orphans that state; flag it.

### 5. `recheck` — after the author pushes

Do not re-read the diff. For each item in the existing review, re-run the
reproduction it gave on the new head and report pass/fail per item. If the review
gave no reproduction, construct one before judging, and say you did.

## Output

Draft in the reply, for the user; say who it is written for in one line, then the
review ready to paste. Shape, in this order:

1. **Verdict in one sentence**: merge / merge after X / don't merge yet, and why.
2. **Verified** — what was run and passed, so the author knows what is already
   confirmed. Lead with this.
3. **Must fix before merge**, numbered, each with the command or `file:line`
   that found it.
4. **Should fix** and **Minor**.
5. **Answers to the author's own open questions**, if the PR asks any.
6. **Decisions for the user**, separated — things only they can call (cut a
   release off this, publish timing, a product trade-off), never buried among
   findings.

Plain language throughout. The user usually wants **CAR form** per finding
(Context: what happens now; Action: the change; Result: what becomes true).
Use it when asked, and for any review they will forward. Strengths named
specifically, not generically. No internal code names a reader cannot look up.
The user is usually the PR's author, and GitHub refuses `--request-changes` on
your own PR, so post as `--comment`.

When posting, name the mirror:
`gh pr review N --repo $R --request-changes|--comment|--approve --body-file -`.
"Changes requested" only for must-fix items the author can act on; "comment" when
the verdict is informational. After a subsequent merge, **sync the other mirror**
(see "This repo's shape").

## Known gotchas

- `#N` is a different PR on each mirror — always name `--repo`.
- `git fetch origin <short-sha>` → "couldn't find remote ref". Fetch the branch.
- Bare `pytest`/`mypy`/`ruff` may hit the stale-path venv shebang — use
  `.venv/bin/python -m <tool>`.
- `gh` defaults to arango-solutions; pass `--repo` for anything on the ArthurKeen
  mirror.
- PyPI's `/pypi/<name>/json` is CDN-cached and lags minutes; verify a published
  version on the simple index instead.
- A merge on GitHub updates only that mirror; the dual push-remote is what keeps
  both in step — sync after merging.
