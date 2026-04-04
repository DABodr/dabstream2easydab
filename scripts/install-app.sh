#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-${USER:-$(id -un)}}"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [[ -z "$TARGET_HOME" ]]; then
    TARGET_HOME="$HOME"
fi

if [[ -n "${SUDO_USER:-}" ]]; then
    TARGET_XDG_DATA_HOME="$TARGET_HOME/.local/share"
    TARGET_XDG_BIN_HOME="$TARGET_HOME/.local/bin"
else
    TARGET_XDG_DATA_HOME="${XDG_DATA_HOME:-$TARGET_HOME/.local/share}"
    TARGET_XDG_BIN_HOME="${XDG_BIN_HOME:-$TARGET_HOME/.local/bin}"
fi

INSTALL_ROOT="$TARGET_XDG_DATA_HOME/dabstream2easydab"
APP_DIR="$INSTALL_ROOT/app"
BUILD_DIR="$INSTALL_ROOT/build"
TOOLS_DIR="$INSTALL_ROOT/tools/bin"
VENV_DIR="$INSTALL_ROOT/venv"
LAUNCHER_DIR="$TARGET_XDG_BIN_HOME"
LAUNCHER_PATH="$LAUNCHER_DIR/dabstream2easydab"
DESKTOP_DIR="$TARGET_XDG_DATA_HOME/applications"
DESKTOP_PATH="$DESKTOP_DIR/dabstream2easydab.desktop"

ETI_TOOLS_REPO="https://github.com/piratfm/eti-tools.git"
ODR_EDI2EDI_REPO="https://github.com/Opendigitalradio/ODR-EDI2EDI.git"

APT_PACKAGES=(
    ca-certificates
    python3
    python3-venv
    python3-pip
    python3-gi
    gir1.2-gtk-3.0
    python3-zmq
    git
    build-essential
    cmake
    autoconf
    automake
    libtool
    pkg-config
    libzmq3-dev
    libfec-dev
)

DRY_RUN=0
SKIP_APT=0
SKIP_TOOLS=0
SKIP_APP=0
FORCE_REBUILD=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Installe dabstream2easydab, ses dependances systeme et les outils externes
necessaires (edi2eti, eti2zmq, odr-edi2edi).

Options:
  --dry-run         Affiche les actions sans rien modifier.
  --skip-apt        N'installe pas les paquets systeme via apt.
  --skip-tools      Ne reconstruit pas les outils externes.
  --skip-app        Ne reinstalle pas l'application ni le lanceur.
  --force-rebuild   Reclone et recompile les outils externes.
  -h, --help        Affiche cette aide.

Exemple:
  ./scripts/install-app.sh
EOF
}

log() {
    printf '%s\n' "$*"
}

warn() {
    printf 'warning: %s\n' "$*" >&2
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
        printf 'commande requise absente: %s\n' "$name" >&2
        exit 1
    fi
}

sudo_prefix() {
    if [[ "$EUID" -eq 0 ]]; then
        return 0
    fi
    require_command sudo
    printf 'sudo\n'
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

install_apt_dependencies() {
    require_command apt-get
    local sudo_cmd
    sudo_cmd="$(sudo_prefix || true)"

    if [[ -n "$sudo_cmd" ]]; then
        run "$sudo_cmd" apt-get update
        run "$sudo_cmd" apt-get install -y "${APT_PACKAGES[@]}"
    else
        run apt-get update
        run apt-get install -y "${APT_PACKAGES[@]}"
    fi
}

install_file() {
    local source_path="$1"
    local target_path="$2"
    run mkdir -p "$(dirname "$target_path")"
    run install -m 755 "$source_path" "$target_path"
}

repair_ownership() {
    if [[ -z "${SUDO_USER:-}" ]]; then
        return
    fi

    local path
    for path in "$INSTALL_ROOT" "$LAUNCHER_PATH" "$DESKTOP_PATH"; do
        if [[ -e "$path" ]]; then
            run chown -R "$TARGET_USER:$TARGET_GROUP" "$path"
        fi
    done
}

copy_application_tree() {
    run rm -rf "$APP_DIR"
    run mkdir -p "$APP_DIR"
    run cp -a "$ROOT_DIR/src" "$APP_DIR/src"
    run cp -a "$ROOT_DIR/pyproject.toml" "$APP_DIR/pyproject.toml"
    run cp -a "$ROOT_DIR/README.md" "$APP_DIR/README.md"
}

build_eti_tools() {
    local repo_dir="$BUILD_DIR/eti-tools"

    clone_or_update_repo "$ETI_TOOLS_REPO" "$repo_dir"
    run make -C "$repo_dir" cleanapps
    run make -C "$repo_dir" \
        "CFLAGS=-O2 -Wall -I. -DHAVE_ZMQ -DHAVE_FEC" \
        "LDFLAGS=-lm -lzmq -lfec" \
        edi2eti eti2zmq

    install_file "$repo_dir/edi2eti" "$TOOLS_DIR/edi2eti"
    install_file "$repo_dir/eti2zmq" "$TOOLS_DIR/eti2zmq"
}

build_odr_edi2edi() {
    local repo_dir="$BUILD_DIR/ODR-EDI2EDI"
    local binary_path=""
    local jobs="1"

    if command -v nproc >/dev/null 2>&1; then
        jobs="$(nproc)"
    fi

    clone_or_update_repo "$ODR_EDI2EDI_REPO" "$repo_dir"
    run_shell "cd \"$repo_dir\" && ./bootstrap.sh"
    run_shell "cd \"$repo_dir\" && ./configure"
    run make -C "$repo_dir" -j"$jobs"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        install_file "$repo_dir/odr-edi2edi" "$TOOLS_DIR/odr-edi2edi"
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
        printf "binaire odr-edi2edi introuvable apres compilation dans %s\n" "$repo_dir" >&2
        exit 1
    fi

    install_file "$binary_path" "$TOOLS_DIR/odr-edi2edi"
}

install_python_app() {
    run mkdir -p "$INSTALL_ROOT"
    copy_application_tree

    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        run python3 -m venv --system-site-packages "$VENV_DIR"
    fi

    run "$VENV_DIR/bin/python" -m pip install --no-deps --force-reinstall "$APP_DIR"
}

write_launcher() {
    run mkdir -p "$LAUNCHER_DIR"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "+ write $LAUNCHER_PATH"
        return
    fi
    cat >"$LAUNCHER_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export DABSTREAM_EDI2ETI="$TOOLS_DIR/edi2eti"
export DABSTREAM_ODR_EDI2EDI="$TOOLS_DIR/odr-edi2edi"
export DABSTREAM_ETI2ZMQ="$TOOLS_DIR/eti2zmq"
exec "$VENV_DIR/bin/python" -m dabstream2easydab "\$@"
EOF
    chmod 755 "$LAUNCHER_PATH"
}

write_desktop_entry() {
    run mkdir -p "$DESKTOP_DIR"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "+ write $DESKTOP_PATH"
        return
    fi
    cat >"$DESKTOP_PATH" <<EOF
[Desktop Entry]
Type=Application
Name=dabstream2easydab
Comment=Relais ETI et conversion EDI vers EasyDABV2
Exec=$LAUNCHER_PATH
Icon=$APP_DIR/src/dabstream2easydab/assets/logo.svg
Terminal=false
Categories=AudioVideo;Network;
EOF
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)
                DRY_RUN=1
                ;;
            --skip-apt)
                SKIP_APT=1
                ;;
            --skip-tools)
                SKIP_TOOLS=1
                ;;
            --skip-app)
                SKIP_APP=1
                ;;
            --force-rebuild)
                FORCE_REBUILD=1
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                printf 'option inconnue: %s\n' "$1" >&2
                usage >&2
                exit 1
                ;;
        esac
        shift
    done

    require_command python3
    if [[ "$SKIP_TOOLS" -eq 0 ]]; then
        require_command git
        require_command make
    fi

    if [[ -n "${SUDO_USER:-}" ]]; then
        warn "script lance avec sudo, installation cible: $TARGET_USER ($TARGET_HOME)"
    fi

    run mkdir -p "$BUILD_DIR" "$TOOLS_DIR"

    if [[ "$SKIP_APT" -eq 0 ]]; then
        install_apt_dependencies
    fi

    if [[ "$SKIP_TOOLS" -eq 0 ]]; then
        build_eti_tools
        build_odr_edi2edi
    fi

    if [[ "$SKIP_APP" -eq 0 ]]; then
        install_python_app
        write_launcher
        write_desktop_entry
    fi

    repair_ownership

    log
    log "Installation terminee."
    log "Utilisateur cible : $TARGET_USER"
    log "Lanceur : $LAUNCHER_PATH"
    log "Desktop : $DESKTOP_PATH"
    log "Outils : $TOOLS_DIR"
}

main "$@"
