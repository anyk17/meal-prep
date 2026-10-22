# Migration plan: go-live on the NAS

How it works: [ARCHITECTURE.md](../ARCHITECTURE.md). How to run things: [DEVELOPMENT.md](../DEVELOPMENT.md). Past phases and the reasoning behind them: [HISTORY.md](HISTORY.md).

## Where we are

The app is feature-complete and runs in a container on the workstation (WSL, `127.0.0.1:8080`). It covers planning by week and month, swaps, shopping lists with long-lasting/fresh groups and bought marking, login for changes, and Publish to https://jflopezo91.github.io/meal-plan/. GitHub builds an attested image for every push to `feature/cloud-migration`.

**What's left is 6d:** run the app on the NAS and make releases a deliberate, verifiable step from the workstation.

## Target

```
 workstation (orchestrator)        GitHub                         NAS (UGREEN DXP2800, UGOS Pro)
 git push main ─────────────▶ CI: tests → image → GHCR digest + attestation
 scripts/release.sh            trust gate: CI green · digest for this commit · attestation verified
                               → digest into deploy/docker-compose.yml → commit + tag vYYYY.MM.DD[.N]
 Komodo Core (127.0.0.1:9120)  procedure: backup ──▶ deploy by digest ──▶ /healthz + revision ──▶ Periphery
```

- **Release** = a git tag on `main`. `release.sh` never builds images; it only approves one that GitHub built and attested.
- **Komodo** runs on the workstation as Core plus its database, with pinned versions. Its **Periphery** agent runs on the NAS. Core calls Periphery, never the other way, and only the workstation's address may reach Periphery.
- **Rollback** = redeploy the previous release. A DB backup is taken before every release.
- **LAN access:** plain HTTP on `http://<nas>:8080`, like Jellyfin. Viewing is open; changes need a login (`MEALPLANNER_COOKIE_SECURE=0`).
- **Portable:** the orchestrator is code under `ops/`, and its secrets sit in a password-manager recovery kit. `release.sh --direct` works without Komodo.
- **Not doing:** deploy-on-push, auto-update (Watchtower), a GitHub runner on the NAS, inbound ports or tunnels.

## Decide before starting

| Question | Proposal |
|---|---|
| Fixed addresses | DHCP reservations for the NAS and the Windows host. WSL reaches the LAN through the Windows host's address, so that is the one the NAS firewall allows. |
| Komodo version | The latest stable 2.x on the day, pinned in `ops/komodo/compose.yaml`. Follow that version's docs for Core↔Periphery auth. |
| Off-NAS backup target | One place that survives a NAS failure: the workstation, a USB disk, or a cloud drive. |
| Password manager entry | One "meal-prep CD recovery kit" item. |

## Steps

Each step ends in a check, and a step that fails stops the go-live. Steps 1–3 change only GitHub and the workstation; the NAS is untouched until step 4.

### 1. Merge to `main`
- **Do:** with explicit go-ahead, merge `feature/cloud-migration` into `main`. Change `container.yml` to build on `main` (not on tags or PRs) and pin `runs-on: ubuntu-24.04`. Upgrade `gh` to a version that has `gh attestation`.
- **Done when:** CI is green on `main`, and `gh attestation verify --owner jflopezo91 oci://ghcr.io/jflopezo91/meal-prep@sha256:<digest>` passes for that commit's image.

### 2. `scripts/release.sh`
- **Do:** write the trust gate: on a clean `main` that is in sync with GitHub, check that CI passed for `HEAD`, find the GHCR digest for `HEAD`, verify its attestation, write the digest into `deploy/docker-compose.yml`, commit "release vYYYY.MM.DD[.N]", tag and push. Make `deploy/docker-compose.yml` the NAS file: LAN port `8080:8080`, `MEALPLANNER_COOKIE_SECURE=0`, data and secret under `/volume1/docker/meal-prep/`.
- **Done when:** a dry run on `main` prints the digest it would release. It refuses a dirty tree, an unpushed commit, a red CI run and a digest that fails verification. Each refusal is checked once.

### 3. Komodo Core on the workstation (`ops/`)
- **Do:** add `ops/komodo/compose.yaml` (official file, pinned, UI on `127.0.0.1:9120`), `compose.env.example` (no secrets), resources as TOML (server, stack, procedure), `ops/bootstrap.sh` (checks Docker and `gh`, writes `compose.env`, starts Core, runs the Resource Sync) and `ops/README.md`. Save the admin login to the recovery kit.
- **Done when:** `ops/bootstrap.sh` takes a clean machine to a running Core that shows the declared resources, with the server still unreachable.

### 4. NAS prep
- **Do:** enable SSH (only for setup). Create `/volume1/docker/meal-prep/{data,secrets}` with `chown 10001:10001`. Deploy a throwaway Compose project that only prints a mounted `secrets:` file. If that does not work, use a read-only bind mount instead.
- **Done when:** the throwaway project prints the file as uid 10001 and is then removed.

### 5. Periphery on the NAS
- **Do:** run Periphery as one UGOS project (pinned version). Add a NAS firewall rule so its port accepts only the workstation's address. Pair it with Core, and save the key/passkey to the recovery kit.
- **Done when:** Core shows the NAS server as healthy, and the Periphery port is refused from another LAN device (a phone).

### 6. Publish key on the NAS
- **Do:** generate the key on the NAS: `ssh-keygen -t ed25519 -N '' -f secrets/deploy_key`, then `chown 10001:10001` and `chmod 600`. Add only its public half to `meal-plan` as a write deploy key. The private half never leaves the NAS and is not in the recovery kit.
- **Done when:** both keys (WSL and NAS) are listed on `meal-plan`. The WSL key is revoked in step 7.

### 7. First release and data move
- **Do:**
  1. `release.sh` the first version.
  2. Stop the WSL container so nothing changes, then `db_cli backup`.
  3. Copy the backup to the NAS and restore it into `data/`.
  4. Run the Komodo procedure.
  5. Log in from a phone on the Wi-Fi and publish one month from the NAS.
  6. Delete the WSL deploy key from `meal-plan` and from `var/secrets/`. Real data now lives only on the NAS.
- **Done when:** `/healthz` reports the release commit. The shopping list in progress ("Oct 19 – Nov 15") shows the same bought state as before the move. The Publish job reaches `live`. A publish from WSL now fails.

### 8. Limits
- **Do:** time a month fill on the N100 and watch memory. Set container memory and CPU limits with headroom, and raise the solver timeout if the N100 needs it.
- **Done when:** a month fill finishes within the timeout under the limits, and the numbers are recorded in `ops/README.md`.

### 9. Backups
- **Do:** add a scheduled `db_cli backup` on the NAS, copied to the off-NAS target and kept for N days.
- **Done when:** a restore of last night's backup into a clean throwaway container shows the same plans and lists.

### 10. Release and rollback drill
- **Do:** release a trivial change, then roll back to the previous tag through Komodo. Also do one deploy with `release.sh --direct`.
- **Done when:** each path ends healthy on the expected revision, with data intact.

### 11. Orchestrator move rehearsal
- **Do:** on a fresh WSL distro or another PC, install Docker and `gh`, clone the repo and run `ops/bootstrap.sh` with the kit. Allow the new address in the firewall and re-pair if asked.
- **Done when:** the new Core shows the running release and can deploy it. Afterwards, remove the temporary firewall rule and turn NAS SSH off.

### 12. Exposure check and close-out
- **Do:** confirm there is no port forward, tunnel or UPnP mapping and no exposed NAS admin endpoint. Confirm that only `8080` (app) and the firewalled Periphery port listen. Load the public site from mobile data. Finish `ops/README.md`: release, rollback, restore, migrate and the orchestrator move.
- **Done when:** a phone off the Wi-Fi sees the public site but cannot reach the NAS, and `ops/README.md` covers every procedure above.

**6d exit:**
- The household uses the app on the NAS.
- Releases go only through `release.sh` and Komodo, by digest.
- Backups restore.
- The orchestrator can be rebuilt from git plus the kit.

## After go-live (optional)

- HTTPS on the LAN with Caddy and a DNS-challenge certificate, then `MEALPLANNER_TRUSTED_PROXIES=1` and secure cookies. No app change is needed.
- Delete the retired Pages site of `meal-prep`: `gh api -X DELETE repos/jflopezo91/meal-prep/pages`.
- Product polish:
  - Dinner-carb choice for linked meals.
  - The `bistec_a_caballo` data fix.
  - Tidy the redundant "Edit weeks" rows.
  - A regression test for creating a list across months.
- Review the reported npm audit findings.

## Decisions in force

| Topic | Decision |
|---|---|
| Hosting | Flask + SQLite in one container on the NAS. LAN only, with no inbound route or tunnel. |
| Access | Viewing is open on the LAN; changes and publishing need a household login. |
| Public site | `jflopezo91/meal-plan` holds data only; its workflow builds the frontend at the pinned `app-ref`. Everything published is public. |
| Secrets | One: the publish deploy key, a file on the NAS that can write to `meal-plan` only. |
| Images | Built and attested by GitHub, and deployed by digest only. |
| Releases | A tag on `main`, gated by `release.sh` and deployed by Komodo. Rollback = the previous release. |
| Planning | Independent weekly plans with no cross-week rules. Rules are advisory for swaps. |
| Shopping lists | Cells for each week and group (long-lasting/fresh), either to buy or bought. There is no pantry or purchase tracking. |
