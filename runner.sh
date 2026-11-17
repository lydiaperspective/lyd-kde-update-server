
#!/bin/bash
set -eo pipefail

# --------------------------------------------------
# Environment
# --------------------------------------------------

if [ ! -f /workspace/.env ]; then
    echo "ERROR: /workspace/.env not found!"
    exit 1
fi

set -a
source /workspace/.env
set +a

FORK_URL="${FORK_REPO:?FORK_REPO is required}"
UPSTREAM_URL="${UPSTREAM_REPO:?UPSTREAM_REPO is required}"
TARGET_BRANCH="${TARGET_BRANCH:?TARGET_BRANCH is required}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:?UPSTREAM_BRANCH is required}"
POLL_INTERVAL="${POLL_INTERVAL:?POLL_INTERVAL is required}"

WORKDIR="/workspace/src-$TARGET_BRANCH"
OUTPUT_DIR="/workspace/output/$TARGET_BRANCH"

# System update feed
SYSUPDATE_ROOT="$OUTPUT_DIR/sysupdate"
FEED_DIR="$SYSUPDATE_ROOT/v2"

# --------------------------------------------------
# GPG configuration
# --------------------------------------------------

GPG_HOME="/gnupg"
GPG_EXPORT_DIR="/key-export"

GPG_KEY_NAME="${GPG_KEY_NAME:?GPG_KEY_NAME is required}"
GPG_KEY_EMAIL="${GPG_KEY_EMAIL:-}"
GPG_KEY_EXPIRE="${GPG_KEY_EXPIRE:-0}"

GPG_SIGNING_KEY="${GPG_SIGNING_KEY:-}"
GPG_PUBLIC_KEY_FILE="${GPG_PUBLIC_KEY_FILE:-kde-linux-signing-public.asc}"

FINGERPRINT_FILE="$GPG_HOME/signing-key-fingerprint"
PUBLIC_KEY_PATH="$GPG_EXPORT_DIR/$GPG_PUBLIC_KEY_FILE"

# Store the last successful build SHA inside Git's metadata.
LAST_BUILT_FILE="$WORKDIR/.git/last-successful-build"

mkdir -p "$WORKDIR" "$OUTPUT_DIR" "$GPG_HOME" "$GPG_EXPORT_DIR"
chmod 700 "$GPG_HOME"

# --------------------------------------------------
# GPG key initialization
# --------------------------------------------------

get_secret_key_fingerprints() {
    gpg --homedir "$GPG_HOME" \
        --batch \
        --with-colons \
        --list-secret-keys 2>/dev/null |
        awk -F: '
            $1 == "sec" {
                want_fingerprint = 1
                next
            }

            want_fingerprint && $1 == "fpr" {
                print $10
                want_fingerprint = 0
            }
        '
}

echo
echo "=================================================="
echo "==> Initializing GPG signing..."
echo "=================================================="

if [ -z "$GPG_SIGNING_KEY" ]; then

    # First preference: previously saved fingerprint.
    if [ -f "$FINGERPRINT_FILE" ]; then
        GPG_SIGNING_KEY=$(cat "$FINGERPRINT_FILE")
        echo "==> Found saved signing fingerprint."

    else
        # Otherwise, look for an existing secret key.
        mapfile -t EXISTING_KEYS < <(get_secret_key_fingerprints)

        if [ "${#EXISTING_KEYS[@]}" -eq 1 ]; then
            GPG_SIGNING_KEY="${EXISTING_KEYS[0]}"
            echo "==> Found an existing secret key. Reusing it."

        elif [ "${#EXISTING_KEYS[@]}" -gt 1 ]; then
            echo "ERROR: Multiple secret keys found in $GPG_HOME."
            echo "Set GPG_SIGNING_KEY in .env to select one."
            exit 1

        else
            # No key exists: generate a new signing key.
            echo "==> No signing key found. Generating a new Ed25519 key..."

            KEY_CONFIG=$(mktemp)

            {
                echo "Key-Type: eddsa"
                echo "Key-Curve: ed25519"
                echo "Key-Usage: sign"
                echo "Name-Real: $GPG_KEY_NAME"

                if [ -n "$GPG_KEY_EMAIL" ]; then
                    echo "Name-Email: $GPG_KEY_EMAIL"
                fi

                echo "Expire-Date: $GPG_KEY_EXPIRE"
                echo "%no-protection"
                echo "%commit"
            } > "$KEY_CONFIG"

            if ! gpg --homedir "$GPG_HOME" \
                --batch \
                --generate-key "$KEY_CONFIG"; then

                rm -f "$KEY_CONFIG"
                echo "ERROR: GPG key generation failed."
                exit 1
            fi

            rm -f "$KEY_CONFIG"

            mapfile -t GENERATED_KEYS < <(get_secret_key_fingerprints)

            if [ "${#GENERATED_KEYS[@]}" -ne 1 ]; then
                echo "ERROR: Could not identify generated signing key."
                exit 1
            fi

            GPG_SIGNING_KEY="${GENERATED_KEYS[0]}"

            echo "==> New signing key generated."
        fi
    fi
fi

# Verify that the selected secret key exists.
if ! gpg --homedir "$GPG_HOME" \
    --batch \
    --list-secret-keys "$GPG_SIGNING_KEY" >/dev/null 2>&1; then

    echo "ERROR: Signing key $GPG_SIGNING_KEY was not found."
    exit 1
fi

# Persist the fingerprint.
printf '%s\n' "$GPG_SIGNING_KEY" > "$FINGERPRINT_FILE"
chmod 600 "$FINGERPRINT_FILE"

echo "==> Signing key fingerprint: $GPG_SIGNING_KEY"

# --------------------------------------------------
# Export public key to host
# --------------------------------------------------

echo "==> Exporting public key..."

PUBLIC_KEY_TEMP="$PUBLIC_KEY_PATH.tmp"

if ! gpg --homedir "$GPG_HOME" \
    --batch \
    --armor \
    --export "$GPG_SIGNING_KEY" > "$PUBLIC_KEY_TEMP"; then

    rm -f "$PUBLIC_KEY_TEMP"
    echo "ERROR: Public key export failed."
    exit 1
fi

if [ ! -s "$PUBLIC_KEY_TEMP" ]; then
    rm -f "$PUBLIC_KEY_TEMP"
    echo "ERROR: Exported public key is empty."
    exit 1
fi

chmod 644 "$PUBLIC_KEY_TEMP"
mv -f "$PUBLIC_KEY_TEMP" "$PUBLIC_KEY_PATH"

echo "==> Public key exported to: $PUBLIC_KEY_PATH"
echo "==> GPG initialization complete."

# --------------------------------------------------
# Startup information
# --------------------------------------------------

echo
echo "=================================================="
echo "KDE Linux Build Server"
echo "=================================================="
echo "Target branch:   $TARGET_BRANCH"
echo "Upstream branch: $UPSTREAM_BRANCH"
echo "Poll interval:   $POLL_INTERVAL seconds"
echo "Fork:            $FORK_URL"
echo "Upstream:        $UPSTREAM_URL"
echo "Output:          $OUTPUT_DIR"
echo "Signing key:     $GPG_SIGNING_KEY"
echo "=================================================="

# --------------------------------------------------
# Repository setup
# --------------------------------------------------

if [ ! -d "$WORKDIR/.git" ]; then
    echo "==> Cloning fork repository..."
    git clone "$FORK_URL" "$WORKDIR"
fi

cd "$WORKDIR"

# Ensure both remotes exist.
if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "$FORK_URL"
fi

if ! git remote get-url upstream >/dev/null 2>&1; then
    git remote add upstream "$UPSTREAM_URL"
fi

# Configure identity for automatic merge commits.
git config user.name "KDE Linux Build Server"
git config user.email "build-server@localhost"

# Fetch before setting up the local branch.
git fetch --prune origin
git fetch --prune upstream

# Ensure the requested branch exists on the fork.
if ! git show-ref --verify --quiet "refs/remotes/origin/$TARGET_BRANCH"; then
    echo "ERROR: Branch origin/$TARGET_BRANCH does not exist."
    exit 1
fi

# Create the local branch only if it doesn't already exist.
if ! git show-ref --verify --quiet "refs/heads/$TARGET_BRANCH"; then
    git switch --create "$TARGET_BRANCH" --track "origin/$TARGET_BRANCH"
else
    git switch "$TARGET_BRANCH"
fi

# --------------------------------------------------
# Build script override
# --------------------------------------------------

restore_build_script() {
    local backup="$WORKDIR/.build.sh.upstream-backup"

    if [ -f "$backup" ]; then
        echo "==> Restoring upstream build.sh..."
        mv -f "$backup" "$WORKDIR/build.sh"
    fi
}

run_build() {
    echo "==> Applying build-server build.sh override..."

    local backup="$WORKDIR/.build.sh.upstream-backup"

    # Restore any leftover backup from an interrupted previous run.
    restore_build_script

    # Save the version belonging to the currently merged Git commit.
    if ! git show HEAD:build.sh > "$backup"; then
        echo "ERROR: Could not back up upstream build.sh."
        rm -f "$backup"
        return 1
    fi

    # Replace the repository version with our build-server version.
    if ! cp /usr/local/bin/kde-linux-build.sh "$WORKDIR/build.sh"; then
        echo "ERROR: Could not install build-server build.sh."
        restore_build_script
        return 1
    fi

    chmod +x "$WORKDIR/build.sh"

    local status=0

    echo "==> Running overridden build.sh..."

    ./build.sh || status=$?

    # Restore the original file regardless of build success.
    restore_build_script

    return "$status"
}

# Restore the original file if the runner exits unexpectedly.
trap restore_build_script EXIT

# --------------------------------------------------
# Artifact publishing
# --------------------------------------------------

publish_artifacts() {
    echo
    echo "==> Publishing build artifacts..."

    if [ ! -d mkosi.output ]; then
        echo "ERROR: mkosi.output does not exist."
        return 1
    fi

    local efi_files=()
    local erofs_files=()
    local caibx_files=()
    local file
    local filename
    local version

    shopt -s nullglob

    for file in mkosi.output/kde-linux_*.efi; do
        filename="${file##*/}"

        if [[ "$filename" =~ ^kde-linux_([0-9]+)\.efi$ ]]; then
            efi_files+=("$file")
        fi
    done

    erofs_files=(mkosi.output/kde-linux_*_root-x86-64.erofs)
    caibx_files=(mkosi.output/kde-linux_*_root-x86-64.caibx)

    shopt -u nullglob

    if [ "${#efi_files[@]}" -eq 0 ]; then
        echo "ERROR: No versioned EFI artifacts found."
        return 1
    fi

    if [ "${#erofs_files[@]}" -eq 0 ]; then
        echo "ERROR: No root EROFS artifacts found."
        return 1
    fi

    if [ "${#caibx_files[@]}" -eq 0 ]; then
        echo "ERROR: No root CAIBX artifacts found."
        return 1
    fi

    # --------------------------------------------------
    # Publish normal build output.
    # Preserve sysupdate because it is managed separately.
    # --------------------------------------------------

    echo "==> Publishing normal build output..."

    if ! rsync -av --delete \
        --exclude='/sysupdate/' \
        mkosi.output/ "$OUTPUT_DIR/"; then

        echo "ERROR: Normal artifact publishing failed."
        return 1
    fi

    # --------------------------------------------------
    # Prepare system update feed.
    # --------------------------------------------------

    echo "==> Preparing system update feed..."

    if ! mkdir -p "$SYSUPDATE_ROOT"; then
        echo "ERROR: Could not create system update directory."
        return 1
    fi

    local stage_dir
    local backup_dir=""

    stage_dir=$(mktemp -d "$SYSUPDATE_ROOT/.v2-stage.XXXXXX") || {
        echo "ERROR: Could not create feed staging directory."
        return 1
    }

    # Copy existing feed to preserve previous versions.
    if [ -d "$FEED_DIR" ]; then
        if ! cp -a "$FEED_DIR/." "$stage_dir/"; then
            echo "ERROR: Could not stage existing feed."
            rm -rf "$stage_dir"
            return 1
        fi
    fi

    # Copy newly built artifacts.
    if ! cp -- "${efi_files[@]}" "${erofs_files[@]}" \
        "${caibx_files[@]}" "$stage_dir/"; then

        echo "ERROR: Could not copy new artifacts into feed."
        rm -rf "$stage_dir"
        return 1
    fi

    # --------------------------------------------------
    # Retain newest four EFI versions.
    # --------------------------------------------------

    echo "==> Applying four-version retention policy..."

    local versions=()
    local old_versions=()

    for file in "$stage_dir"/kde-linux_*.efi; do
        [ -f "$file" ] || continue

        filename="${file##*/}"

        if [[ "$filename" =~ ^kde-linux_([0-9]+)\.efi$ ]]; then
            versions+=("${BASH_REMATCH[1]}")
        fi
    done

    if [ "${#versions[@]}" -eq 0 ]; then
        echo "ERROR: No EFI versions available in staged feed."
        rm -rf "$stage_dir"
        return 1
    fi

    mapfile -t old_versions < <(
        printf '%s\n' "${versions[@]}" |
            sort -rn |
            tail -n +5
    )

    for version in "${old_versions[@]}"; do
        echo "==> Removing expired version: $version"

        rm -f \
            "$stage_dir/kde-linux_${version}.efi" \
            "$stage_dir"/kde-linux_"${version}"_root-x86-64.*
    done

    # --------------------------------------------------
    # Generate SHA256SUMS.
    # --------------------------------------------------

    echo "==> Generating SHA256SUMS..."

    (
        cd "$stage_dir" || exit 1
        LC_ALL=C sha256sum kde-linux_* > SHA256SUMS
    ) || {
        echo "ERROR: Checksum generation failed."
        rm -rf "$stage_dir"
        return 1
    }

    # --------------------------------------------------
    # Sign SHA256SUMS.
    # --------------------------------------------------

    echo "==> Signing SHA256SUMS..."

    if ! gpg --homedir "$GPG_HOME" \
        --batch \
        --yes \
        --local-user "$GPG_SIGNING_KEY" \
        --detach-sign \
        --output "$stage_dir/SHA256SUMS.gpg" \
        "$stage_dir/SHA256SUMS"; then

        echo "ERROR: GPG signing failed."
        rm -rf "$stage_dir"
        return 1
    fi

    # Verify signature before publishing.
    echo "==> Verifying feed signature..."

    if ! gpg --homedir "$GPG_HOME" \
        --verify \
        "$stage_dir/SHA256SUMS.gpg" \
        "$stage_dir/SHA256SUMS"; then

        echo "ERROR: Feed signature verification failed."
        rm -rf "$stage_dir"
        return 1
    fi

    # --------------------------------------------------
    # Replace existing feed.
    # --------------------------------------------------

    echo "==> Publishing signed system update feed..."

    if [ -d "$FEED_DIR" ]; then
        backup_dir="$SYSUPDATE_ROOT/.v2-backup.$$"

        if ! mv "$FEED_DIR" "$backup_dir"; then
            echo "ERROR: Could not move existing feed."
            rm -rf "$stage_dir"
            return 1
        fi
    fi

    if ! mv "$stage_dir" "$FEED_DIR"; then
        echo "ERROR: Could not publish staged feed."

        if [ -n "$backup_dir" ] && [ -d "$backup_dir" ]; then
            mv "$backup_dir" "$FEED_DIR" || \
                echo "WARNING: Could not restore previous feed!"
        fi

        rm -rf "$stage_dir"
        return 1
    fi

    if [ -n "$backup_dir" ]; then
        rm -rf "$backup_dir"
    fi

    echo "==> System update feed published successfully."

    return 0
}

# --------------------------------------------------
# Main polling loop
# --------------------------------------------------

while true; do
    echo
    echo "=================================================="
    echo "==> [$TARGET_BRANCH] Checking for updates..."
    echo "==> $(date -u)"
    echo "=================================================="

    if ! git fetch --prune origin; then
        echo "ERROR: Could not fetch fork."
        sleep "$POLL_INTERVAL"
        continue
    fi

    if ! git fetch --prune upstream; then
        echo "ERROR: Could not fetch upstream."
        sleep "$POLL_INTERVAL"
        continue
    fi

    # --------------------------------------------------
    # Integrate fork changes.
    # --------------------------------------------------

    echo "==> Checking fork updates..."

    if ! git merge --no-edit "origin/$TARGET_BRANCH"; then
        echo "ERROR: Could not merge fork updates."
        git merge --abort 2>/dev/null || true
        echo "==> Skipping this build cycle."
        sleep "$POLL_INTERVAL"
        continue
    fi

    # --------------------------------------------------
    # Integrate upstream KDE Linux changes.
    # --------------------------------------------------

    echo "==> Checking upstream updates..."

    if ! git merge --no-edit "upstream/$UPSTREAM_BRANCH"; then
        echo "ERROR: Upstream merge conflict or merge failure."
        git merge --abort 2>/dev/null || true
        echo "==> Skipping this build cycle."
        sleep "$POLL_INTERVAL"
        continue
    fi

    CURRENT_SHA=$(git rev-parse HEAD)

    echo "==> Current source commit: $CURRENT_SHA"

    # --------------------------------------------------
    # Skip already successful builds.
    # --------------------------------------------------

    LAST_BUILT_SHA=""

    if [ -f "$LAST_BUILT_FILE" ]; then
        LAST_BUILT_SHA=$(cat "$LAST_BUILT_FILE")
    fi

    if [ "$CURRENT_SHA" = "$LAST_BUILT_SHA" ]; then
        echo "==> No new commits. Build already up to date."
        sleep "$POLL_INTERVAL"
        continue
    fi

    # --------------------------------------------------
    # Build
    # --------------------------------------------------

    echo "==> New commits detected. Starting build..."

    if run_build; then
        echo "==> Build completed successfully!"

        if publish_artifacts; then

            # Only mark the commit successful after publishing.
            if printf '%s\n' "$CURRENT_SHA" > "$LAST_BUILT_FILE.tmp" &&
                mv "$LAST_BUILT_FILE.tmp" "$LAST_BUILT_FILE"; then

                echo
                echo "=================================================="
                echo "==> BUILD AND PUBLISH SUCCESSFUL"
                echo "==> Commit: $CURRENT_SHA"
                echo "=================================================="

            else
                echo "ERROR: Could not update successful build marker."
                echo "The build will be retried next cycle."
            fi

        else
            echo "ERROR: Publishing failed."
            echo "The build will be retried next cycle."
        fi

    else
        echo "ERROR: Build failed!"
        echo "Existing published artifacts will be preserved."
        echo "The build will be retried next cycle."
    fi

    echo "==> Sleeping for $POLL_INTERVAL seconds..."
    sleep "$POLL_INTERVAL"
done
