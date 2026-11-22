#!/bin/sh
# Daemons Companion installer. This installs the app, not a daemon or its services.
# Keep execution inside one function: a truncated download must not partially run.
daemons_install() (
    set -eu
    umask 077
    daemon_repo=jpacifico/daemon-harness
    daemon_tag=companion-v0.11.20
    daemon_asset=Daemons-Companion-macOS-arm64.dmg
    daemon_sha=589a51e00099e1f59324b8c8705b80fd15c9177f6abc963821632cbcaf7a29de
    daemon_dest="$HOME/Applications"
    daemon_package=
    daemon_launch=yes
    daemon_verify_only=no
    daemon_tmp=
    daemon_stage=
    daemon_backup=
    daemon_target=
    daemon_mounted=no
    daemon_swapped=no
    daemon_placed=no
    daemon_download_pid=

    daemon_fail() { printf 'Daemons: %s\n' "$*" >&2; exit 1; }
    daemon_cleanup() {
        daemon_status=$?
        trap - 0 HUP INT TERM
        if [ -n "$daemon_download_pid" ]; then
            kill "$daemon_download_pid" 2>/dev/null || true
            wait "$daemon_download_pid" 2>/dev/null || true
        fi
        if [ "$daemon_mounted" = yes ]; then
            /usr/bin/hdiutil detach "$daemon_tmp/mount" -quiet || true
        fi
        if [ "$daemon_swapped" = no ] && [ "$daemon_placed" = yes ]; then /bin/rm -rf "$daemon_target"; fi
        if [ "$daemon_swapped" = no ] && [ -n "$daemon_backup" ] && [ -d "$daemon_backup/Daemons Companion.app" ]; then
            /bin/mv "$daemon_backup/Daemons Companion.app" "$daemon_target" || {
                printf 'Daemons: previous app preserved at %s\n' "$daemon_backup" >&2
                daemon_backup=
            }
        fi
        if [ -n "$daemon_stage" ]; then /bin/rm -rf "$daemon_stage"; fi
        if [ -n "$daemon_backup" ]; then /bin/rm -rf "$daemon_backup"; fi
        if [ -n "$daemon_tmp" ]; then /bin/rm -rf "$daemon_tmp"; fi
        exit "$daemon_status"
    }
    trap daemon_cleanup 0
    trap 'exit 130' HUP INT TERM
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --destination) [ "$#" -ge 2 ] || daemon_fail 'Missing destination'; daemon_dest=$2; shift 2 ;;
            --package) [ "$#" -ge 2 ] || daemon_fail 'Missing package'; daemon_package=$2; shift 2 ;;
            --no-launch) daemon_launch=no; shift ;;
            --verify-only) daemon_verify_only=yes; shift ;;
            *) daemon_fail "Unknown option: $1" ;;
        esac
    done
    [ "$(/usr/bin/uname -s)" = Darwin ] || daemon_fail 'This package requires macOS.'
    [ "$(/usr/bin/uname -m)" = arm64 ] || daemon_fail 'Daemons Companion requires an Apple Silicon Mac.'
    [ "$(/usr/bin/id -u)" -ne 0 ] || daemon_fail 'Run as your normal user, without sudo.'
    case "$daemon_dest" in /*) ;; *) daemon_fail 'Destination must be an absolute path.' ;; esac
    [ ! -L "$daemon_dest" ] || daemon_fail 'The destination cannot be a symbolic link.'
    daemon_tmp=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/daemons-app-download.XXXXXXXX")

    if [ -z "$daemon_package" ]; then
        daemon_package="$daemon_tmp/$daemon_asset"
        printf 'Downloading Daemons Companion (145 MB)...\n'
        # A public release downloads directly. A signed-in GitHub CLI also
        # reaches a release the account can read but the public cannot.
        if ! /usr/bin/curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --progress-bar \
            "https://github.com/$daemon_repo/releases/download/$daemon_tag/$daemon_asset" \
            --output "$daemon_package"; then
            /bin/rm -f "$daemon_package"
            if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
                daemon_fail 'Download failed. Check your internet connection, then run this command again.'
            fi
            gh release download "$daemon_tag" --repo "$daemon_repo" --pattern "$daemon_asset" --output "$daemon_package" &
            daemon_download_pid=$!
            while kill -0 "$daemon_download_pid" 2>/dev/null; do
                printf '.'
                sleep 1
            done
            wait "$daemon_download_pid" || daemon_fail 'Download failed. Check your internet connection, then run this command again.'
            daemon_download_pid=
            printf '\n'
        fi
    fi
    [ -f "$daemon_package" ] && [ ! -L "$daemon_package" ] || daemon_fail 'Package must be a regular file.'
    printf 'Verifying download...\n'
    daemon_actual=$(/usr/bin/shasum -a 256 "$daemon_package")
    daemon_actual=${daemon_actual%% *}
    [ "$daemon_actual" = "$daemon_sha" ] || daemon_fail 'Download checksum mismatch. Nothing was installed.'
    if [ "$daemon_verify_only" = yes ]; then printf 'Download verified.\n'; exit 0; fi

    /bin/mkdir -p "$daemon_dest"
    daemon_dest=$(cd -P "$daemon_dest" && pwd)
    daemon_target="$daemon_dest/Daemons Companion.app"
    [ ! -L "$daemon_target" ] || daemon_fail 'An existing symbolic link occupies the app destination.'
    if [ -e "$daemon_target" ]; then
        daemon_existing=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$daemon_target/Contents/Info.plist" 2>/dev/null || true)
        [ "$daemon_existing" = org.daemonharness.companion ] || daemon_fail 'An unrelated item occupies the app destination.'
        if /bin/ps -axo comm= | /usr/bin/grep -F "$daemon_target/Contents/MacOS/Daemons Companion" >/dev/null; then
            daemon_fail 'Close Daemons Companion, then run this command again. Your daemon can keep running.'
        fi
    fi
    printf 'Installing the app in %s...\n' "$daemon_dest"
    /bin/mkdir "$daemon_tmp/mount"
    /usr/bin/hdiutil attach "$daemon_package" -readonly -nobrowse -mountpoint "$daemon_tmp/mount" -quiet
    daemon_mounted=yes
    daemon_source="$daemon_tmp/mount/Daemons Companion.app"
    [ -d "$daemon_source" ] && [ ! -L "$daemon_source" ] || daemon_fail 'The app is missing from the verified disk image.'
    [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$daemon_source/Contents/Info.plist")" = org.daemonharness.companion ] || daemon_fail 'Unexpected application identity.'
    /usr/bin/codesign --verify --deep --strict "$daemon_source"
    daemon_stage=$(/usr/bin/mktemp -d "$daemon_dest/.daemons-companion-stage.XXXXXXXX")
    /usr/bin/ditto "$daemon_source" "$daemon_stage/Daemons Companion.app"
    /usr/bin/codesign --verify --deep --strict "$daemon_stage/Daemons Companion.app"
    /usr/bin/hdiutil detach "$daemon_tmp/mount" -quiet
    daemon_mounted=no
    if [ -e "$daemon_target" ]; then
        daemon_backup=$(/usr/bin/mktemp -d "$daemon_dest/.daemons-companion-backup.XXXXXXXX")
        /bin/mv "$daemon_target" "$daemon_backup/Daemons Companion.app"
    fi
    /bin/mv "$daemon_stage/Daemons Companion.app" "$daemon_target"
    daemon_placed=yes
    /usr/bin/codesign --verify --deep --strict "$daemon_target"
    daemon_swapped=yes
    printf 'Daemons Companion installed: %s\n' "$daemon_target"
    if [ "$daemon_launch" = yes ]; then
        printf 'Opening the app. Continue setup in its window.\n'
        /usr/bin/open "$daemon_target" || daemon_fail 'The app is installed but could not open. Open it from Applications.'
    fi
)
daemons_install "$@"
