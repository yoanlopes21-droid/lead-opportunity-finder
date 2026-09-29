#!/bin/zsh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/macos_launcher.applescript"
USER_APPLICATIONS="${HOME}/Applications"
TARGET="$USER_APPLICATIONS/Lead Opportunity Finder.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "Erreur : le launcher .app ne peut être construit que sur macOS."
  exit 1
fi
if [[ ! -x /usr/bin/osacompile ]]; then
  print -u2 "Erreur : osacompile est indisponible sur ce Mac."
  exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
  print -u2 "Erreur : source AppleScript introuvable."
  exit 1
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
GENERATED_SOURCE="$TEMP_DIR/launcher.applescript"
TEMP_APP="$TEMP_DIR/Lead Opportunity Finder.app"

ESCAPED_ROOT="${PROJECT_ROOT//\\/\\\\}"
ESCAPED_ROOT="${ESCAPED_ROOT//\"/\\\"}"
ESCAPED_ROOT="${ESCAPED_ROOT//&/\\&}"
ESCAPED_ROOT="${ESCAPED_ROOT//|/\\|}"
/usr/bin/sed "s|__PROJECT_ROOT__|$ESCAPED_ROOT|g" "$TEMPLATE" > "$GENERATED_SOURCE"
/usr/bin/osacompile -o "$TEMP_APP" "$GENERATED_SOURCE"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.leadopportunityfinder.local" "$TEMP_APP/Contents/Info.plist"
for key in NSAppleEventsUsageDescription NSAppleMusicUsageDescription NSCalendarsUsageDescription NSCameraUsageDescription NSContactsUsageDescription NSHomeKitUsageDescription NSMicrophoneUsageDescription NSPhotoLibraryUsageDescription NSRemindersUsageDescription NSSiriUsageDescription NSSystemAdministrationUsageDescription; do
  /usr/libexec/PlistBuddy -c "Delete :$key" "$TEMP_APP/Contents/Info.plist" >/dev/null 2>&1 || true
done
case "$PROJECT_ROOT" in
  "$HOME/Desktop"/*)
    /usr/libexec/PlistBuddy -c "Add :NSDesktopFolderUsageDescription string Lead Opportunity Finder doit accéder au projet local placé sur le Bureau." "$TEMP_APP/Contents/Info.plist"
    ;;
  "$HOME/Documents"/*)
    /usr/libexec/PlistBuddy -c "Add :NSDocumentsFolderUsageDescription string Lead Opportunity Finder doit accéder au projet local placé dans Documents." "$TEMP_APP/Contents/Info.plist"
    ;;
  "$HOME/Downloads"/*)
    /usr/libexec/PlistBuddy -c "Add :NSDownloadsFolderUsageDescription string Lead Opportunity Finder doit accéder au projet local placé dans Téléchargements." "$TEMP_APP/Contents/Info.plist"
    ;;
esac
/usr/bin/codesign --force --deep --sign - --identifier com.leadopportunityfinder.local "$TEMP_APP"
/bin/mkdir -p "$USER_APPLICATIONS"
if [[ -e "$TARGET" ]]; then
  PREVIOUS="$TEMP_DIR/previous.app"
  /bin/mv "$TARGET" "$PREVIOUS"
fi
if ! /bin/mv "$TEMP_APP" "$TARGET"; then
  [[ -e "${PREVIOUS:-}" ]] && /bin/mv "$PREVIOUS" "$TARGET"
  print -u2 "Erreur : impossible de remplacer le launcher ; la version précédente a été restaurée."
  exit 1
fi
print "Launcher créé : $TARGET"
