#!/usr/bin/env bash
# build.sh — compile the Gojo Rust EKF as an Android arm64 .so and copy it
# into the Android Studio project so it's picked up by Gradle automatically.
#
# Prerequisites (run once):
#   cargo install cargo-ndk
#   rustup target add aarch64-linux-android
#   export ANDROID_NDK_HOME=/path/to/your/ndk   # e.g. ~/Android/Sdk/ndk/27.x.x
#
# Usage:
#   ./build.sh             — release build (default)
#   ./build.sh --debug     — debug build (larger .so, includes symbols)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUST_DIR="$SCRIPT_DIR/motion_tracker_rs"
JNILIBS_DIR="$SCRIPT_DIR/android/app/src/main/jniLibs/arm64-v8a"

# ── Argument parsing ──────────────────────────────────────────────────────────
BUILD_PROFILE="release"
CARGO_PROFILE_FLAG="--release"
if [[ "${1:-}" == "--debug" ]]; then
    BUILD_PROFILE="debug"
    CARGO_PROFILE_FLAG=""
fi

# ── Prerequisite checks ───────────────────────────────────────────────────────
if ! command -v cargo-ndk &>/dev/null; then
    echo "ERROR: cargo-ndk not found."
    echo ""
    echo "Install with:"
    echo "  cargo install cargo-ndk"
    echo "  rustup target add aarch64-linux-android"
    echo "  export ANDROID_NDK_HOME=/path/to/android/ndk"
    exit 1
fi

if ! rustup target list --installed 2>/dev/null | grep -q "aarch64-linux-android"; then
    echo "ERROR: Android ARM64 target not installed."
    echo ""
    echo "Install with:"
    echo "  rustup target add aarch64-linux-android"
    exit 1
fi

if [[ -z "${ANDROID_NDK_HOME:-}" ]]; then
    echo "ERROR: ANDROID_NDK_HOME is not set."
    echo ""
    echo "Set it to your NDK directory, e.g.:"
    echo "  export ANDROID_NDK_HOME=\$HOME/Android/Sdk/ndk/27.2.12479018"
    exit 1
fi

# ── Build ─────────────────────────────────────────────────────────────────────
echo "Building Rust EKF library for Android arm64 ($BUILD_PROFILE)..."
cd "$RUST_DIR"

# --lib ensures only the library is compiled — the Termux binary targets
# (motion_tracker, replay, dashboard) are not built for Android.
cargo ndk -t arm64-v8a build $CARGO_PROFILE_FLAG --lib

# ── Copy ──────────────────────────────────────────────────────────────────────
SO_SRC="$RUST_DIR/target/aarch64-linux-android/$BUILD_PROFILE/libmotion_tracker_rs.so"

if [[ ! -f "$SO_SRC" ]]; then
    echo "ERROR: Expected .so not found at:"
    echo "  $SO_SRC"
    exit 1
fi

mkdir -p "$JNILIBS_DIR"
# Rename to match System.loadLibrary("gojo_core") in GojoJni.kt
cp "$SO_SRC" "$JNILIBS_DIR/libgojo_core.so"

SO_SIZE=$(du -h "$JNILIBS_DIR/libgojo_core.so" | cut -f1)
echo ""
echo "Done — libgojo_core.so ($SO_SIZE) copied to:"
echo "  $JNILIBS_DIR/"
echo ""
echo "Open the android/ project in Android Studio and run on device."
