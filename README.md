# KDE Linux Update Manager

This is an update-manager addition for the existing `lyd-kde-update-server` project. It replaces the old single-branch polling runner with a queue-driven worker and adds a password-protected WebUI.

## What is included

- Dashboard with queued/running/completed/failed/cancelled builds, log view, and manual start/stop.
- Multiple independent build jobs, one per fork branch. Each job has its own name, upstream branch, schedule interval, ISO setting, enabled/disabled state, and next-run time.
- Scheduled builds are disabled by default. Enabling a job schedules its first run one full interval later; disabling it cancels a queued scheduled build but does not interrupt a build already running.
- Manual “Run now” is available for disabled or enabled jobs. Jobs are serialized through one worker so privileged mkosi builds do not compete for loop devices, memory, or disk space.
- The worker checks only enabled jobs when their individual next-run time is due. It fetches and merges the fork and upstream branches; it skips the actual build when the resulting commit matches the last successfully published commit.
- Build history, commit SHA, previous successful SHA, and `git diff --name-status` file changes.
- Settings for branches, upstream branch, build interval, update-feed retention, history retention, and bootable ISO creation.
- GPG-signed `SHA256SUMS` and atomic replacement of each branch's `sysupdate/v2` feed, preserving the existing feed if the staged feed fails validation.
- Cancellation sends a signal to the build process group. The local `build.sh` patch adds an EXIT cleanup trap to unmount the temporary ESP and remove incomplete per-build/cache files.
- Manager is bound to host `127.0.0.1:5055`; Tailscale Serve publishes it privately to the tailnet with HTTPS. It is not added to the Caddy public artifact server.

## Install into the existing project

1. Stop the old Compose project without deleting its named volumes:

   ```bash
   podman compose -f Compose.yml down
   ```

   Do **not** add `-v`. This preserves `build_artifacts`, `build_state`, and `build_gnupg`.

2. Back up your existing `.env` and project directory. Copy the files from this bundle over the matching files in your project directory. Keep your existing `.env`; do not replace it with `sample.env`.

3. Add the following three settings to your existing `.env` and choose your own values:

   ```dotenv
   MANAGER_USERNAME=admin
   MANAGER_PASSWORD=use-a-long-unique-password-here
   SESSION_SECRET=replace-with-a-long-random-secret
   ```

   Generate a session secret with:

   ```bash
   openssl rand -hex 32
   ```

   `MANAGER_PASSWORD` is used only to initialize the manager's password hash on first startup. The hash is kept in `build_state`; changing the environment variable later does not reset the stored password. Keep `.env` readable only by the account that administers the stack (`chmod 600 .env`).

4. Ensure the existing `.env` still has `GITHUB_USERNAME`, `GITHUB_TOKEN`, `FORK_REPO`, `UPSTREAM_REPO`, `TARGET_BRANCH`, `UPSTREAM_BRANCH`, `POLL_INTERVAL`, `GPG_KEY_NAME`, and `GPG_EXPORT_DIR`. The GitHub token is passed to Git through a temporary environment-level URL rewrite; it is not displayed in the WebUI.

5. Build and start the new services:

   ```bash
   podman compose -f Compose.yml up -d --build
   podman compose -f Compose.yml ps
   ```

   The new worker uses `build_sources` for multi-branch checkouts and also mounts the old `build_source` volume read-only as a migration source. It copies that existing checkout (including its last-successful-build marker) into the new source layout for the configured primary branch, so it can avoid an unnecessary first rebuild. Existing artifact, state, and GPG volumes retain their original Compose volume keys and are reused. The first manager request initializes the SQLite schema and hashes the initial password. The worker waits/retries until that initialization exists.

6. Check the manager locally on the build host first:

   ```bash
   curl -I http://127.0.0.1:5055/login
   ```

## Publish the WebUI only to Tailscale

On the build host, run:

```bash
tailscale serve --bg 5055
tailscale serve status
```

Tailscale Serve provides an HTTPS URL available within your tailnet. Open the URL printed by the command and sign in with `MANAGER_USERNAME` and `MANAGER_PASSWORD`. Tailscale Serve requires HTTPS certificates to be enabled for the tailnet; its first run may prompt you to enable them. Do not use `tailscale funnel` for this service.

The Compose port is deliberately `127.0.0.1:5055:5055`, so it is not listening on the LAN interface. Tailscale Serve proxies to that loopback listener. The existing Caddy artifact server on port `8485` is unchanged and remains separate from the manager.

To stop sharing the manager through Tailscale:

```bash
tailscale serve --https=443 off
```

## Configure multiple build jobs

Sign in and open **Build jobs**. Add one job for each fork branch that you want to build, for example `main` and `lyd-kde-linux-server`. Each job has its own display name, fork branch, upstream branch, interval in hours, and ISO option. A branch can have only one job.

- New jobs are **disabled by default** and do not trigger a build when created.
- Click **Enable** to schedule the first automatic build one full interval from that moment. The next-run time is shown in UTC.
- Click **Disable** to prevent future scheduled builds and cancel a waiting scheduled build. A build already running is allowed to finish.
- Click **Run now** to queue a manual build even when the schedule is disabled.
- Delete a job to remove its schedule. Existing build history and published artifacts are retained. A job cannot be deleted while its build is running.
- Schedules are independent, but the worker runs only one build at a time. If several jobs become due, they are processed in turn.

Open **Settings** for the default upstream branch used by new jobs, per-branch update-feed retention, and build-history retention. Existing jobs keep their own upstream branch and schedule. The ISO option is set per job: when disabled, the build skips live ESP creation, live-root image creation, ISO assembly, and torrent creation while still producing normal system-update artifacts and the signed feed.

When upgrading an older manager database, its previously configured branches are imported as disabled jobs. This prevents the upgrade from unexpectedly starting scheduled builds.

## Output layout

Artifacts remain under the existing `build_artifacts` named volume and are served by Caddy on port `8485`. Each configured branch is published beneath its branch path, e.g.:

```text
/workspace/output/<branch>/
  kde-linux_*.efi
  kde-linux_*.iso              # only when ISO is enabled
  ...
  sysupdate/v2/
    kde-linux_*.efi
    kde-linux_*_root-x86-64.erofs
    kde-linux_*_root-x86-64.caibx
    SHA256SUMS
    SHA256SUMS.gpg
```

The public artifact URL therefore remains `http://<server>:8485/<branch>/...`, with `sysupdate/v2` below the branch directory.

## Important implementation notes

- The worker uses SQLite in the existing `build_state` volume. Build logs are stored there too, so the manager can read them without mounting the host filesystem.
- The manager has no Podman socket and cannot issue arbitrary shell commands. It writes validated build requests to SQLite; the builder worker executes only the fixed build workflow.
- Branch names are validated with Git's `check-ref-format` before they are used as paths.
- The ISO/cancellation patch is applied to a temporary copy of the checked-out `build.sh`, not the Git worktree. That avoids leaving a modified tracked file that would interfere with later fetch/merge operations.
- If a future upstream/fork `build.sh` changes enough that the patch no longer applies, that build will fail before starting `build.sh`; inspect the worker log and update `builder/build-iso-cancel.patch` against the new script rather than bypassing the check.
- This first version is intentionally single-worker. It reports the current build state and live log, but does not claim a percentage estimate where `mkosi` does not provide one.

## Troubleshooting

```bash
podman compose -f Compose.yml logs -f manager
podman compose -f Compose.yml logs -f builder
podman compose -f Compose.yml logs -f webserver
```

If the manager password was initialized incorrectly, do not delete the whole state volume (it also contains build logs and history). A password-reset utility/UI is not included in this initial version; preserve a backup of the state volume before making any manual database change.
