#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="$ROOT/Dockerfile"
DOCKERIGNORE="$ROOT/.dockerignore"
FLYCTL="${FLYCTL:-$HOME/.fly/bin/flyctl}"

if [[ ! -x "$FLYCTL" && -x "$HOME/.fly/bin/fly" ]]; then
    FLYCTL="$HOME/.fly/bin/fly"
fi
if [[ ! -x "$FLYCTL" ]]; then
    echo "flyctl not found at $FLYCTL" >&2
    exit 1
fi

runs=()
while IFS= read -r profiles_dir; do
    run_dir="${profiles_dir%/05_profiles/profiles}"
    run="$(basename "$run_dir")"
    if [[ "$run" =~ [[:space:]] ]]; then
        echo "run name contains whitespace and cannot be used in Dockerfile COPY: $run" >&2
        exit 1
    fi
    runs+=("$run")
done < <(find "$ROOT/runs" -type d -path "$ROOT/runs/*/05_profiles/profiles" | sort)

if [[ ${#runs[@]} -eq 0 ]]; then
    echo "No valid runs found under runs/*/05_profiles/profiles" >&2
    exit 1
fi

replace_block() {
    local file="$1"
    local begin="$2"
    local end="$3"
    local block="$4"
    local tmp
    local block_file

    if ! grep -q "^$begin$" "$file" || ! grep -q "^$end$" "$file"; then
        echo "Missing $begin / $end markers in $file" >&2
        exit 1
    fi

    tmp="$(mktemp "$file.tmp.XXXXXX")"
    block_file="$(mktemp "$file.block.XXXXXX")"
    printf "%s" "$block" > "$block_file"
    if ! awk -v begin="$begin" -v end="$end" -v block_file="$block_file" '
        $0 == begin {
            print
            while ((getline line < block_file) > 0) {
                print line
            }
            close(block_file)
            skip = 1
            next
        }
        $0 == end { skip = 0 }
        !skip { print }
    ' "$file" > "$tmp"; then
        rm -f "$tmp" "$block_file"
        exit 1
    fi
    mv "$tmp" "$file"
    rm -f "$block_file"
}

data_lines=""
ignore_lines=$'runs/*\n'
for run in "${runs[@]}"; do
    data_lines+="COPY runs/$run/05_profiles/profiles /app/runs/$run/05_profiles/profiles"$'\n'
    contacts="$ROOT/runs/$run/05_profiles/contacts"
    if [[ -d "$contacts" ]]; then
        data_lines+="COPY runs/$run/05_profiles/contacts /app/runs/$run/05_profiles/contacts"$'\n'
    fi

    ignore_lines+="!runs/$run/"$'\n'
    ignore_lines+="runs/$run/*"$'\n'
    ignore_lines+="!runs/$run/05_profiles/"$'\n'
    ignore_lines+="runs/$run/05_profiles/*"$'\n'
    ignore_lines+="!runs/$run/05_profiles/profiles/"$'\n'
    ignore_lines+="!runs/$run/05_profiles/profiles/**"$'\n'
    if [[ -d "$contacts" ]]; then
        ignore_lines+="!runs/$run/05_profiles/contacts/"$'\n'
        ignore_lines+="!runs/$run/05_profiles/contacts/**"$'\n'
    fi
done

replace_block "$DOCKERFILE" "# DATA-BEGIN" "# DATA-END" "$data_lines"
replace_block "$DOCKERIGNORE" "# RUNS-BEGIN" "# RUNS-END" "$ignore_lines"

echo "Including runs: ${runs[*]}"
"$FLYCTL" deploy
