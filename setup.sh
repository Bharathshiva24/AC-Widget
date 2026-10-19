#!/bin/bash
# One-time setup: pick your AC brand, save its credentials in the macOS Keychain
# and install the app. Credentials never touch disk — only the Keychain.
#
#   bash setup.sh            hide the password while typing
#   bash setup.sh --show     show it, to check for typos
set -u
DIR="$HOME/ac-control"
cd "$DIR" 2>/dev/null || { echo "Can't find $DIR"; exit 1; }
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
[ -n "$PY" ] || { echo "Python 3 is needed but was not found."; exit 1; }

echo "AC setup"
echo

# The brand list comes from the acbrands package, so adding a brand there is
# all it takes for it to appear here.
BRANDS="$("$PY" -c 'import acbrands
for b in acbrands.catalogue():
    print("|".join([b["id"], b["label"], b["connectivity"], b["notes"]]))' 2>/dev/null)"
[ -n "$BRANDS" ] || { echo "Could not read the brand list. Is $DIR/acbrands present?"; exit 1; }

CURRENT="$("$PY" -c 'import acbrands; print(acbrands.selected_brand())' 2>/dev/null)"
echo "Which air conditioner do you have?"
i=0
while IFS='|' read -r id label conn notes; do
    i=$((i+1))
    ids[$i]="$id"
    mark=" "; [ "$id" = "$CURRENT" ] && mark="*"
    printf "  %s %d) %-22s %s\n" "$mark" "$i" "$label" "$conn"
    [ -n "$notes" ] && printf "       %s\n" "$notes"
done <<< "$BRANDS"
echo "   (* = currently selected)"
printf "Number [1]: "
read -r choice
choice="${choice:-1}"
BRAND="${ids[$choice]:-}"
[ -n "$BRAND" ] || { echo "That is not one of the options. Nothing was changed."; exit 1; }
echo

# Each brand says which credentials it needs; we ask for exactly those.
FIELDS="$("$PY" -c 'import acbrands, sys
for f in acbrands.describe(sys.argv[1])["fields"]:
    print("|".join([f["name"], f["label"], "1" if f["secret"] else "0", f["example"]]))' "$BRAND")"

if [ -z "$FIELDS" ]; then
    echo "This brand needs no credentials."
else
    while IFS='|' read -r name label secret example; do
        [ -n "$name" ] || continue
        hint=""; [ -n "$example" ] && hint=" (e.g. $example)"
        if [ "$secret" = "1" ] && [ "${1:-}" != "--show" ]; then
            read -r -s -p "$label$hint: " value; echo
        else
            read -r -p "$label$hint: " value
        fi
        [ -n "$value" ] || { echo "$label is required. Nothing was changed."; exit 1; }
        /usr/bin/security add-generic-password -U -s ac-panel -a "$BRAND.$name" -w "$value" \
            || { echo "Could not save to the Keychain."; exit 1; }
    done <<< "$FIELDS"
    echo "Saved in your Keychain."
fi

"$PY" -c 'import acbrands, sys; acbrands.write_config(brand=sys.argv[1])' "$BRAND" || exit 1
echo "Brand set to $BRAND."
bash "$DIR/install.sh"
