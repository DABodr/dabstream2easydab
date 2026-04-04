#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_NAME="dabstream2easydab"
APP_NAME="dabstream2easydab"
INSTALL_PREFIX="/usr/lib/$PACKAGE_NAME"
ICON_NAME="$APP_NAME"
DEFAULT_OUTPUT_DIR="$ROOT_DIR/dist"
WORK_ROOT="$ROOT_DIR/.deb-build"
TOOL_NAMES=(edi2eti eti2zmq odr-edi2edi)
DEBIAN_BUILD_PACKAGES=(
    build-essential
    git
    autoconf
    automake
    libtool
    pkg-config
    dpkg-dev
)
DEBIAN_TOOL_BUILD_PACKAGES=(
    libzmq3-dev
    libfec-dev
)

ETI_TOOLS_REPO="https://github.com/piratfm/eti-tools.git"
ODR_EDI2EDI_REPO="https://github.com/Opendigitalradio/ODR-EDI2EDI.git"

FORCE_REBUILD=0
SKIP_TOOLS=0
DRY_RUN=0
OUTPUT_DIR="$DEFAULT_OUTPUT_DIR"
REVISION="1"
REQUESTED_ARCH=""
TOOL_DIR=""
MAINTAINER="${DEBFULLNAME:-dabstream2easydab contributors}"
if [[ -n "${DEBEMAIL:-}" ]]; then
    MAINTAINER="$MAINTAINER <$DEBEMAIL>"
else
    MAINTAINER="$MAINTAINER <noreply@example.com>"
fi

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Build a native Debian package for dabstream2easydab.

The package includes:
- the GTK application
- the desktop launcher and menu entry
- the application icon
- bundled copies of edi2eti, eti2zmq, and odr-edi2edi

Supported Debian architectures:
- i386  (32-bit x86 / "x32" in common speech)
- amd64 (64-bit x86 / x64)
- arm64

Options:
  --arch ARCH        Target architecture alias. Accepted values:
                     i386, x32, x86, amd64, x64, x86_64, arm64, aarch64.
                     Cross-architecture builds are not supported.
  --output-dir DIR   Directory where the .deb file will be written.
  --tool-dir DIR     Directory containing prebuilt edi2eti, eti2zmq, and
                     odr-edi2edi binaries for the target architecture.
  --maintainer TEXT  Override the Maintainer field.
  --revision N       Debian revision suffix, default: 1.
  --skip-tools       Reuse tools from .deb-build, tools/bin, or the system PATH.
  --force-rebuild    Reclone and rebuild external tools from scratch.
  --dry-run          Print the build steps without creating files.
  -h, --help         Show this help message.

Examples:
  ./scripts/build-deb.sh
  ./scripts/build-deb.sh --arch amd64
  ./scripts/build-deb.sh --output-dir release
  ./scripts/build-deb.sh --arch arm64 --skip-tools --tool-dir /path/to/arm64-tools
EOF
}

log() {
    printf '%s\n' "$*"
}

run() {
    log "+ $*"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        "$@"
    fi
}

run_shell() {
    log "+ $*"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        bash -lc "$*"
    fi
}

require_command() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        printf 'required command not found: %s\n' "$name" >&2
        exit 1
    fi
}

normalize_arch() {
    case "$1" in
        amd64|x64|x86_64)
            printf 'amd64\n'
            ;;
        i386|x32|x86|ia32)
            printf 'i386\n'
            ;;
        arm64|aarch64)
            printf 'arm64\n'
            ;;
        *)
            printf 'unsupported architecture: %s\n' "$1" >&2
            exit 1
            ;;
    esac
}

clone_or_update_repo() {
    local repo_url="$1"
    local target_dir="$2"

    if [[ "$FORCE_REBUILD" -eq 1 ]]; then
        run rm -rf "$target_dir"
    fi

    if [[ -d "$target_dir/.git" ]]; then
        run git -C "$target_dir" fetch --tags --prune
        run git -C "$target_dir" pull --ff-only
        return
    fi

    run rm -rf "$target_dir"
    run git clone "$repo_url" "$target_dir"
}

install_executable() {
    local source_path="$1"
    local target_path="$2"
    run mkdir -p "$(dirname "$target_path")"
    run install -m 755 "$source_path" "$target_path"
}

build_eti_tools() {
    local repo_dir="$BUILD_DIR/eti-tools"

    clone_or_update_repo "$ETI_TOOLS_REPO" "$repo_dir"
    run make -C "$repo_dir" cleanapps
    run make -C "$repo_dir" \
        "CFLAGS=-O2 -Wall -I. -DHAVE_ZMQ -DHAVE_FEC" \
        "LDFLAGS=-lm -lzmq -lfec" \
        edi2eti eti2zmq

    install_executable "$repo_dir/edi2eti" "$TOOLS_BUNDLE_DIR/edi2eti"
    install_executable "$repo_dir/eti2zmq" "$TOOLS_BUNDLE_DIR/eti2zmq"
}

build_odr_edi2edi() {
    local repo_dir="$BUILD_DIR/ODR-EDI2EDI"
    local jobs="1"
    local binary_path=""

    if command -v nproc >/dev/null 2>&1; then
        jobs="$(nproc)"
    fi

    clone_or_update_repo "$ODR_EDI2EDI_REPO" "$repo_dir"
    run_shell "cd \"$repo_dir\" && ./bootstrap.sh"
    run_shell "cd \"$repo_dir\" && ./configure"
    run make -C "$repo_dir" -j"$jobs"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        install_executable "$repo_dir/odr-edi2edi" "$TOOLS_BUNDLE_DIR/odr-edi2edi"
        return
    fi

    for candidate in "$repo_dir/odr-edi2edi" "$repo_dir/src/odr-edi2edi"; do
        if [[ -x "$candidate" ]]; then
            binary_path="$candidate"
            break
        fi
    done

    if [[ -z "$binary_path" ]]; then
        binary_path="$(find "$repo_dir" -maxdepth 3 -type f -name 'odr-edi2edi' -print -quit || true)"
    fi

    if [[ -z "$binary_path" ]]; then
        printf 'unable to locate odr-edi2edi after building %s\n' "$repo_dir" >&2
        exit 1
    fi

    install_executable "$binary_path" "$TOOLS_BUNDLE_DIR/odr-edi2edi"
}

ensure_tool_bundle() {
    local tool_name
    populate_tool_bundle_from_existing
    for tool_name in "${TOOL_NAMES[@]}"; do
        if [[ ! -x "$TOOLS_BUNDLE_DIR/$tool_name" ]]; then
            printf 'missing bundled tool: %s\n' "$TOOLS_BUNDLE_DIR/$tool_name" >&2
            printf 'run without --skip-tools or provide %s in tools/bin or in the system PATH.\n' "$tool_name" >&2
            exit 1
        fi
    done
}

populate_tool_bundle_from_existing() {
    local tool_name=""
    local candidate=""
    local source_candidates=()

    for tool_name in "${TOOL_NAMES[@]}"; do
        if [[ -x "$TOOLS_BUNDLE_DIR/$tool_name" ]]; then
            continue
        fi

        source_candidates=(
            "${TOOL_DIR:+$TOOL_DIR/$tool_name}"
            "$ROOT_DIR/tools/bin/$TARGET_ARCH/$tool_name"
            "$ROOT_DIR/tools/bin/$tool_name"
            "$HOME/.local/share/dabstream2easydab/tools/bin/$tool_name"
        )

        candidate=""
        for candidate_path in "${source_candidates[@]}"; do
            if [[ -x "$candidate_path" ]]; then
                candidate="$candidate_path"
                break
            fi
        done

        if [[ -z "$candidate" ]]; then
            candidate="$(command -v "$tool_name" || true)"
        fi

        if [[ -n "$candidate" ]]; then
            install_executable "$candidate" "$TOOLS_BUNDLE_DIR/$tool_name"
        fi
    done
}

binary_matches_arch() {
    local binary_path="$1"
    local expected_arch="$2"
    local description=""

    description="$(file -b "$binary_path" 2>/dev/null || true)"
    case "$expected_arch" in
        amd64)
            [[ "$description" == *"x86-64"* ]]
            ;;
        i386)
            [[ "$description" == *"80386"* || "$description" == *"Intel 80386"* ]]
            ;;
        arm64)
            [[ "$description" == *"aarch64"* || "$description" == *"ARM aarch64"* ]]
            ;;
        *)
            return 1
            ;;
    esac
}

validate_tool_bundle_architecture() {
    local tool_name=""
    local tool_path=""

    require_command file
    for tool_name in "${TOOL_NAMES[@]}"; do
        tool_path="$TOOLS_BUNDLE_DIR/$tool_name"
        if ! binary_matches_arch "$tool_path" "$TARGET_ARCH"; then
            printf 'tool architecture mismatch for %s: expected %s, got:\n' "$tool_name" "$TARGET_ARCH" >&2
            file "$tool_path" >&2 || true
            printf 'use --tool-dir with binaries built for %s.\n' "$TARGET_ARCH" >&2
            exit 1
        fi
    done
}

strip_python_cache() {
    local target_dir="$1"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        return
    fi
    find "$target_dir" -type d -name '__pycache__' -prune -exec rm -rf {} +
    find "$target_dir" -type f -name '*.pyc' -delete
}

read_version() {
    python3 - <<'PY'
from pathlib import Path
import tomllib

data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(data["project"]["version"])
PY
}

build_runtime_depends() {
    local shlibs_output=""
    local shlibs_depends=""
    local tmpdir=""
    local stderr_file=""

    tmpdir="$(mktemp -d)"
    stderr_file="$tmpdir/shlibdeps.stderr"
    mkdir -p "$tmpdir/debian"
    cat >"$tmpdir/debian/control" <<EOF
Source: $PACKAGE_NAME
Section: sound
Priority: optional
Maintainer: $MAINTAINER
Standards-Version: 4.6.2

Package: $PACKAGE_NAME
Architecture: $TARGET_ARCH
Description: temporary control file for dependency analysis
EOF

    if ! shlibs_output="$(
        cd "$tmpdir" && dpkg-shlibdeps -O \
            "$STAGE_DIR$INSTALL_PREFIX/src/dabstream2easydab/_tools/bin/edi2eti" \
            "$STAGE_DIR$INSTALL_PREFIX/src/dabstream2easydab/_tools/bin/eti2zmq" \
            "$STAGE_DIR$INSTALL_PREFIX/src/dabstream2easydab/_tools/bin/odr-edi2edi" \
            2>"$stderr_file"
    )"; then
        cat "$stderr_file" >&2
        rm -rf "$tmpdir"
        return 1
    fi

    if [[ -s "$stderr_file" ]]; then
        sed '/binaries to analyze should already be installed in their package.s directory/d' \
            "$stderr_file" >&2
    fi
    rm -rf "$tmpdir"

    shlibs_depends="$(printf '%s\n' "$shlibs_output" | sed -n 's/^shlibs:Depends=//p')"

    python3 - "$shlibs_depends" <<'PY'
import sys

manual = [
    "python3 (>= 3.11)",
    "python3-gi",
    "gir1.2-gtk-3.0",
    "python3-zmq",
]
merged = []
for chunk in [", ".join(manual), sys.argv[1]]:
    for dep in chunk.split(","):
        item = dep.strip()
        if item and item not in merged:
            merged.append(item)
print(", ".join(merged))
PY
}

write_launcher() {
    local launcher_path="$STAGE_DIR/usr/bin/$APP_NAME"
    run mkdir -p "$(dirname "$launcher_path")"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "+ write $launcher_path"
        return
    fi
    cat >"$launcher_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$INSTALL_PREFIX"
export PYTHONPATH="\$APP_ROOT/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m dabstream2easydab "\$@"
EOF
    chmod 755 "$launcher_path"
}

write_desktop_entry() {
    local desktop_path="$STAGE_DIR/usr/share/applications/$APP_NAME.desktop"
    run mkdir -p "$(dirname "$desktop_path")"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "+ write $desktop_path"
        return
    fi
    cat >"$desktop_path" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=dabstream2easydab
Comment=Relay ETI and convert EDI streams for EasyDABV2
Exec=$APP_NAME
Icon=$ICON_NAME
Terminal=false
Categories=AudioVideo;Audio;
Keywords=DAB;ETI;EDI;EasyDAB;ZeroMQ;
StartupNotify=true
EOF
}

write_maintainer_scripts() {
    local postinst_path="$STAGE_DIR/DEBIAN/postinst"
    local postrm_path="$STAGE_DIR/DEBIAN/postrm"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "+ write $postinst_path"
        log "+ write $postrm_path"
        return
    fi

    cat >"$postinst_path" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
EOF
    chmod 755 "$postinst_path"

    cat >"$postrm_path" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
EOF
    chmod 755 "$postrm_path"
}

write_doc_files() {
    local doc_dir="$STAGE_DIR/usr/share/doc/$PACKAGE_NAME"
    run mkdir -p "$doc_dir"
    run install -m 644 "$ROOT_DIR/README.md" "$doc_dir/README.md"
    run install -m 644 "$ROOT_DIR/LICENSE" "$doc_dir/LICENSE"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "+ write $doc_dir/copyright"
        return
    fi
    cat >"$doc_dir/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: dabstream2easydab
Source: local checkout

Files: *
Copyright: 2026 dabstream2easydab contributors
License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 .
 On Debian systems, the complete text of the GNU General Public License
 version 3 can be found in /usr/share/common-licenses/GPL-3.
EOF
}

stage_application_tree() {
    local app_root="$STAGE_DIR$INSTALL_PREFIX"
    local package_root="$app_root/src/dabstream2easydab"

    run mkdir -p "$app_root"
    run cp -a "$ROOT_DIR/src" "$app_root/src"
    strip_python_cache "$app_root/src"
    run mkdir -p "$package_root/_tools/bin"
    run install -m 644 "$ROOT_DIR/README.md" "$app_root/README.md"
    run install -m 644 "$ROOT_DIR/LICENSE" "$app_root/LICENSE"

    run install -m 755 "$TOOLS_BUNDLE_DIR/edi2eti" "$package_root/_tools/bin/edi2eti"
    run install -m 755 "$TOOLS_BUNDLE_DIR/eti2zmq" "$package_root/_tools/bin/eti2zmq"
    run install -m 755 "$TOOLS_BUNDLE_DIR/odr-edi2edi" "$package_root/_tools/bin/odr-edi2edi"
}

stage_desktop_assets() {
    local icon_dir="$STAGE_DIR/usr/share/icons/hicolor/scalable/apps"

    run mkdir -p "$icon_dir"
    run install -m 644 \
        "$ROOT_DIR/src/dabstream2easydab/assets/logo.svg" \
        "$icon_dir/$ICON_NAME.svg"
    write_desktop_entry
    write_launcher
}

write_control_file() {
    local depends="$1"
    local installed_size="$2"
    local control_path="$STAGE_DIR/DEBIAN/control"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "+ write $control_path"
        return
    fi

    cat >"$control_path" <<EOF
Package: $PACKAGE_NAME
Version: $DEB_VERSION
Section: sound
Priority: optional
Architecture: $TARGET_ARCH
Maintainer: $MAINTAINER
Installed-Size: $installed_size
Depends: $depends
Description: GTK ETI/EDI relay for EasyDABV2
 Small GTK application that receives ETI or EDI streams, converts them when
 needed, and forwards a stable local ETI output over raw TCP or ZeroMQ for
 EasyDABV2 and compatible receivers.
EOF
}

debian_package_installed() {
    local package_name="$1"
    dpkg-query -W -f='${Status}' "$package_name" 2>/dev/null | grep -q 'install ok installed'
}

ensure_debian_build_packages() {
    local packages=("${DEBIAN_BUILD_PACKAGES[@]}")
    local missing=()
    local package_name=""

    if [[ "$SKIP_TOOLS" -eq 0 ]]; then
        packages+=("${DEBIAN_TOOL_BUILD_PACKAGES[@]}")
    fi

    for package_name in "${packages[@]}"; do
        if ! debian_package_installed "$package_name"; then
            missing+=("$package_name")
        fi
    done

    if [[ "${#missing[@]}" -eq 0 ]]; then
        return
    fi

    printf 'missing Debian build dependencies: %s\n' "${missing[*]}" >&2
    printf 'install them with:\n' >&2
    printf '  sudo apt install %s\n' "${missing[*]}" >&2
    if [[ "$SKIP_TOOLS" -eq 0 ]]; then
        printf 'or rerun the package build with --skip-tools if compatible binaries are already available locally.\n' >&2
    fi
    exit 1
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --arch)
                [[ $# -ge 2 ]] || { printf 'missing value for --arch\n' >&2; exit 1; }
                REQUESTED_ARCH="$2"
                shift
                ;;
            --output-dir)
                [[ $# -ge 2 ]] || { printf 'missing value for --output-dir\n' >&2; exit 1; }
                OUTPUT_DIR="$2"
                shift
                ;;
            --tool-dir)
                [[ $# -ge 2 ]] || { printf 'missing value for --tool-dir\n' >&2; exit 1; }
                TOOL_DIR="$2"
                shift
                ;;
            --maintainer)
                [[ $# -ge 2 ]] || { printf 'missing value for --maintainer\n' >&2; exit 1; }
                MAINTAINER="$2"
                shift
                ;;
            --revision)
                [[ $# -ge 2 ]] || { printf 'missing value for --revision\n' >&2; exit 1; }
                REVISION="$2"
                shift
                ;;
            --skip-tools)
                SKIP_TOOLS=1
                ;;
            --force-rebuild)
                FORCE_REBUILD=1
                ;;
            --dry-run)
                DRY_RUN=1
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                printf 'unknown option: %s\n' "$1" >&2
                usage >&2
                exit 1
                ;;
        esac
        shift
    done

    require_command bash
    require_command python3
    require_command dpkg
    require_command dpkg-deb
    require_command dpkg-shlibdeps
    require_command install
    require_command find
    require_command sed
    ensure_debian_build_packages
    require_command git
    require_command make

    HOST_ARCH="$(normalize_arch "$(dpkg --print-architecture)")"
    if [[ -n "$REQUESTED_ARCH" ]]; then
        TARGET_ARCH="$(normalize_arch "$REQUESTED_ARCH")"
    else
        TARGET_ARCH="$HOST_ARCH"
    fi

    if [[ -n "$TOOL_DIR" ]]; then
        TOOL_DIR="$(cd "$TOOL_DIR" && pwd)"
    fi

    if [[ "$TARGET_ARCH" != "$HOST_ARCH" && "$SKIP_TOOLS" -eq 0 ]]; then
        printf 'cross-architecture package assembly is allowed, but native tool builds are not.\n' >&2
        printf 'host architecture: %s, requested: %s\n' "$HOST_ARCH" "$TARGET_ARCH" >&2
        printf 'use --skip-tools with --tool-dir pointing to prebuilt %s binaries.\n' "$TARGET_ARCH" >&2
        exit 1
    fi

    VERSION="$(cd "$ROOT_DIR" && read_version)"
    DEB_VERSION="${VERSION}-${REVISION}"
    ARCH_ROOT="$WORK_ROOT/$TARGET_ARCH"
    BUILD_DIR="$ARCH_ROOT/build"
    TOOLS_BUNDLE_DIR="$ARCH_ROOT/tools/bin"
    STAGE_DIR="$ARCH_ROOT/pkgroot"
    PACKAGE_FILE="$OUTPUT_DIR/${PACKAGE_NAME}_${DEB_VERSION}_${TARGET_ARCH}.deb"

    run mkdir -p "$BUILD_DIR" "$TOOLS_BUNDLE_DIR" "$OUTPUT_DIR"
    run rm -rf "$STAGE_DIR"
    run mkdir -p "$STAGE_DIR/DEBIAN"

    if [[ "$SKIP_TOOLS" -eq 0 ]]; then
        build_eti_tools
        build_odr_edi2edi
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
        ensure_tool_bundle
        validate_tool_bundle_architecture
    fi

    stage_application_tree
    stage_desktop_assets
    write_doc_files
    write_maintainer_scripts

    if [[ "$DRY_RUN" -eq 1 ]]; then
        write_control_file \
            "python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, python3-zmq" \
            "0"
        log
        log "Dry-run complete."
        log "Host architecture  : $HOST_ARCH"
        log "Target architecture: $TARGET_ARCH"
        log "Package path: $PACKAGE_FILE"
        exit 0
    fi

    DEPENDS="$(build_runtime_depends)"
    INSTALLED_SIZE="$(du -sk "$STAGE_DIR/usr" | cut -f1)"
    write_control_file "$DEPENDS" "$INSTALLED_SIZE"

    if command -v desktop-file-validate >/dev/null 2>&1; then
        run desktop-file-validate "$STAGE_DIR/usr/share/applications/$APP_NAME.desktop"
    fi

    run dpkg-deb --build --root-owner-group "$STAGE_DIR" "$PACKAGE_FILE"
    run dpkg-deb --info "$PACKAGE_FILE"

    log
    log "Package created successfully."
    log "Architecture : $TARGET_ARCH"
    log "Output       : $PACKAGE_FILE"
}

main "$@"
