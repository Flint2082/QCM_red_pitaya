#!/usr/bin/env bash
#
# generate_and_push.sh
#
# Convert the Vivado/Model Composer implementation bitstream (top.bit) into the
# Zynq boot binary (top.bit.bin) with Bootgen, store it in the bitstream/ folder,
# then git-commit the bitstream together with the most recently built .fpg file
# and the tracked Simulink models (.slx) it was built from, and push to the
# remote.
#
# Usage:
#   ./generate_and_push.sh "<commit message>"
#
# A commit message is mandatory.
# Paths can be overridden via environment variables (see "SETTINGS" below).

set -euo pipefail

# === ARGUMENTS ===
COMMIT_MSG="${1:-}"
[ -n "$COMMIT_MSG" ] || {
    printf 'Error: a commit message is required.\n' >&2
    printf 'Usage: %s "<commit message>"\n' "$0" >&2
    exit 1
}

# === SETTINGS ===
PROJECT_DIR="${PROJECT_DIR:-$HOME/work/CASPER_repos/qcm_red_pitaya}"
TARGET_DIR="${TARGET_DIR:-$PROJECT_DIR/model_composer/qcm_rp/myproj/myproj.runs/impl_1}"
BITSTREAM_DIR="${BITSTREAM_DIR:-$PROJECT_DIR/bitstream}"
FPG_SEARCH_DIR="${FPG_SEARCH_DIR:-$PROJECT_DIR/model_composer/qcm_rp/outputs}"
MODEL_DIR="${MODEL_DIR:-$PROJECT_DIR/model_composer}"

BITFILE="top.bit"
BIFFILE="top.bif"
OUTPUT_BIN="$BITSTREAM_DIR/top.bit.bin"

# === HELPERS ===
log() { printf '%s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# === SCRIPT START ===

command -v bootgen >/dev/null 2>&1 || die "bootgen not found in PATH. Source the Vivado/Vitis settings script first."

[ -d "$TARGET_DIR" ] || die "Implementation directory not found: $TARGET_DIR"
[ -f "$TARGET_DIR/$BITFILE" ] || die "Bitstream not found: $TARGET_DIR/$BITFILE (has implementation finished?)"

log "📂 Working in implementation directory: $TARGET_DIR"
cd "$TARGET_DIR"

log "📁 Ensuring output directory exists: $BITSTREAM_DIR"
mkdir -p "$BITSTREAM_DIR"

log "📝 Creating BIF file..."
printf 'all:{ %s }' "$TARGET_DIR/$BITFILE" > "$BIFFILE"

log "⚙️  Running Bootgen..."
# In -process_bitstream mode Bootgen ignores -o and writes "<bitfile>.bin" next
# to the input bitstream (i.e. into TARGET_DIR, our current directory), so we
# generate it there and move it into BITSTREAM_DIR afterward.
bootgen -image "$BIFFILE" -arch zynq -process_bitstream bin -w \
    || die "Bootgen failed"

GENERATED_BIN="$TARGET_DIR/$BITFILE.bin"
[ -f "$GENERATED_BIN" ] || die "Bootgen did not produce expected file: $GENERATED_BIN"

log "📦 Moving boot binary into $BITSTREAM_DIR ..."
mv -f "$GENERATED_BIN" "$OUTPUT_BIN"

log "✅ Generated boot binary: $OUTPUT_BIN"

# === GIT COMMIT & PUSH ===
log "🔎 Locating most recent .fpg file under $FPG_SEARCH_DIR ..."
FPG_FILE="$(find "$FPG_SEARCH_DIR" -type f -name '*.fpg' -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | head -n1 | cut -d' ' -f2-)"
[ -n "$FPG_FILE" ] || die "No .fpg file found under $FPG_SEARCH_DIR"
log "   Found: $FPG_FILE"

cd "$PROJECT_DIR" || die "Project directory not found: $PROJECT_DIR"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$PROJECT_DIR is not a git repository"

log "📝 Staging bitstream, .fpg and models ..."
# Keep exactly one .fpg tracked: the server loads the newest .fpg in outputs/ as
# its register map, so stale builds only make that choice ambiguous. Untrack the
# previous one (the file stays on disk here; history keeps every build).
git rm --cached --quiet --ignore-unmatch -- "$FPG_SEARCH_DIR/*.fpg"
# Both artifacts typically live under .gitignore'd build/output dirs, so force-add.
git add -f -- "$OUTPUT_BIN" "$FPG_FILE"

# Commit the models with the bitstream they produced, so the .slx files in git
# always match the hardware; otherwise the newest design exists only in this
# working copy. Only tracked models are staged (-u): a new one is added by hand.
for slx in "$MODEL_DIR"/*.slx; do
    if [ -e "$slx.autosave" ] && [ "$slx.autosave" -nt "$slx" ]; then
        log "⚠️  $(basename "$slx") has unsaved changes in Simulink; only its saved version is committed."
    fi
done
UNTRACKED_MODELS="$(git ls-files --others --exclude-standard -- "$MODEL_DIR/*.slx")"
if [ -n "$UNTRACKED_MODELS" ]; then
    log "⚠️  Not committed, because these models are untracked (git add them once if the design uses them):"
    log "$UNTRACKED_MODELS"
fi
git add -u -- "$MODEL_DIR/*.slx"
MODELS="$(git diff --cached --name-only -- "$MODEL_DIR/*.slx" | sed 's#.*/##' | paste -sd ' ' -)"

if git diff --cached --quiet; then
    log "ℹ️  Nothing to commit — bitstream, .fpg and models are already up to date."
else
    BUILT="$(basename "$OUTPUT_BIN"), $(basename "$FPG_FILE")"
    [ -z "$MODELS" ] || BUILT="$BUILT; models: $MODELS"
    git commit -m "$COMMIT_MSG" \
        -m "build: update bitstream and fpg ($BUILT)" \
        || die "git commit failed"
    log "✅ Committed $BUILT."
fi

log "📤 Pushing to remote..."
git push || die "git push failed"
log "✅ Push complete."
