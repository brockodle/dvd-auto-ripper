#!/bin/bash

###################
# Configuration
###################
DVD_DEVICE="/dev/sr0"
TMDB_API_KEY="b71086e74a0d3b9e48f4537c3a60a874"
TVDB_API_KEY="0288a259-2426-4036-9e8c-cdb198098f1b"
TMDB_API_URL="https://api.themoviedb.org/3"
TVDB_API_URL="https://api4.thetvdb.com/v4"
LOG_DIR="/tmp/dvd_ripping_logs"
SCAN_LOG="$LOG_DIR/handbrake_scan.log"

###################
# Utility Functions
###################
handle_error() {
    echo "$1"
    exit 1
}

ensure_dependencies() {
    local required_commands=("HandBrakeCLI" "curl" "jq" "sed" "dd")
    for cmd in "${required_commands[@]}"; do
        if ! command -v "$cmd" &> /dev/null; then
            handle_error "Required command '$cmd' not found"
        fi
    done
}

ensure_dvd_readable() {
    if [ ! -b "$DVD_DEVICE" ]; then
        handle_error "DVD device $DVD_DEVICE not found"
    fi
    
    if ! dd if="$DVD_DEVICE" of=/dev/null count=1 bs=2048 2>/dev/null; then
        handle_error "Cannot read DVD device $DVD_DEVICE. Check if a disc is inserted."
    fi
}

###################
# DVD Scanning Functions
###################
scan_dvd() {
    mkdir -p "$LOG_DIR"
    echo "Scanning DVD..."
    
    if ! HandBrakeCLI -i "$DVD_DEVICE" --title 0 --scan --min-duration 1 2>&1 | tee "$SCAN_LOG"; then
        handle_error "HandBrake scan failed"
    fi

    if ! grep -q "scan: DVD has" "$SCAN_LOG"; then
        handle_error "Failed to detect DVD structure."
    fi
    echo "DVD scan complete"
}

find_long_titles() {
    local titles=()
    local chapters=()
    local current_title=""
    local current_chapters=0
    local duration_seconds=0
    local min_duration_seconds=3600  # 1 hour in seconds

    while IFS= read -r line; do
        if [[ $line =~ ^[[:space:]]*\+[[:space:]]*title[[:space:]]+([0-9]+): ]]; then
            if [[ -n $current_title && $duration_seconds -ge $min_duration_seconds ]]; then
                titles+=("$current_title")
                chapters+=("$current_chapters")
            fi
            current_title="${BASH_REMATCH[1]}"
            current_chapters=0
            duration_seconds=0
        fi

        if [[ $line =~ ^[[:space:]]*\+[[:space:]]*duration:[[:space:]]+([0-9]{2}):([0-9]{2}):([0-9]{2}) ]] && [[ -n $current_title ]]; then
            local hours=$((10#${BASH_REMATCH[1]}))
            local minutes=$((10#${BASH_REMATCH[2]}))
            local seconds=$((10#${BASH_REMATCH[3]}))
            duration_seconds=$((hours * 3600 + minutes * 60 + seconds))
        fi

        if [[ $line =~ ^[[:space:]]*\+[[:space:]]+([0-9]+):[[:space:]]+duration ]]; then
            ((current_chapters++))
        fi
    done < "$SCAN_LOG"

    if [[ ${#titles[@]} -eq 0 ]]; then
        handle_error "No titles found with duration >= 1 hour"
    fi

    local titles_str=$(IFS=,; echo "${titles[*]}")
    local chapters_str=$(IFS=,; echo "${chapters[*]}")
    echo "${titles_str}:${chapters_str}"
}

###################
# TVDB API Functions (Fixed)
###################
authenticate_tvdb() {
    local auth_response
    auth_response=$(curl -s -X POST "${TVDB_API_URL}/login" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -d "{\"apikey\":\"${TVDB_API_KEY}\"}")

    local token
    token=$(echo "$auth_response" | jq -r '.data.token // empty')
    [[ -z "$token" ]] && handle_error "TVDB authentication failed"
    echo "$token"
}

get_show_info() {
    local show_name="$1"
    local token="$2"

    local search_url="${TVDB_API_URL}/search?query=$(echo "$show_name" | jq -sRr @uri)&type=series"
    local search_response
    search_response=$(curl -s -X GET "$search_url" -H "Authorization: Bearer $token")

    local show_id
    show_id=$(echo "$search_response" | jq -r '.data[] | select(.type=="series") | .id' | head -n1)
    [[ -z "$show_id" ]] && handle_error "Show not found"

    echo "$show_id"
}

get_season_info() {
    local show_id="$1"
    local season_num="$2"
    local token="$3"

    local episodes_url="${TVDB_API_URL}/series/${show_id}/episodes/default?season=${season_num}"
    local episodes_response
    episodes_response=$(curl -s -X GET "$episodes_url" -H "Authorization: Bearer $token")

    local total_episodes
    total_episodes=$(echo "$episodes_response" | jq -r '.data | length')
    [[ $total_episodes -eq 0 ]] && handle_error "No episodes found for season $season_num"

    echo "$total_episodes"
}

###################
# Main Script
###################
main() {
    ensure_dependencies
    ensure_dvd_readable

    read -rp "Enter output directory: " OUTPUT_DIR
    OUTPUT_DIR="${OUTPUT_DIR%/}"
    OUTPUT_DIR="${OUTPUT_DIR/#\~/$HOME}"
    mkdir -p "$OUTPUT_DIR" || handle_error "Cannot create output directory"

    read -rp "Enter show name: " SHOW_NAME
    read -rp "Enter season number: " SEASON_NUM
    read -rp "Enter starting episode number: " STARTING_EPISODE

    [[ ! "$SEASON_NUM" =~ ^[0-9]+$ ]] && handle_error "Invalid season number"
    [[ ! "$STARTING_EPISODE" =~ ^[0-9]+$ ]] && handle_error "Invalid episode number"

    echo "Scanning DVD..."
    scan_dvd

    echo "Finding longest title..."
    IFS=: read -r TITLES_STR CHAPTERS_STR < <(find_long_titles)
    IFS=, read -ra TITLES <<< "$TITLES_STR"
    IFS=, read -ra CHAPTER_COUNTS <<< "$CHAPTERS_STR"

    echo "Authenticating with TVDB..."
    TVDB_TOKEN=$(authenticate_tvdb)

    echo "Fetching show information..."
    SHOW_ID=$(get_show_info "$SHOW_NAME" "$TVDB_TOKEN")

    echo "Fetching season information..."
    TOTAL_EPISODES=$(get_season_info "$SHOW_ID" "$SEASON_NUM" "$TVDB_TOKEN")

    LAST_EPISODE=$((STARTING_EPISODE + CHAPTER_COUNTS[0] - 1))
    if ((LAST_EPISODE > TOTAL_EPISODES)); then
        echo "Episodes will overflow into next season"
        NEXT_SEASON=$((SEASON_NUM + 1))
        mkdir -p "$(dirname "$OUTPUT_DIR")/Season_$(printf "%02d" "$NEXT_SEASON")"
    fi

    EPISODE_NUM=$STARTING_EPISODE
    for ((CHAPTER=1; CHAPTER<=CHAPTER_COUNTS[0]; CHAPTER++)); do
        OUTPUT_FILE="$OUTPUT_DIR/S$(printf "%02d" "$SEASON_NUM")E$(printf "%02d" "$EPISODE_NUM").mkv"

        echo "Ripping episode $EPISODE_NUM from chapter $CHAPTER..."
        HandBrakeCLI -i "$DVD_DEVICE" -o "$OUTPUT_FILE" --title "${TITLES[0]}" --chapters "$CHAPTER" \
            --encoder nvenc_h265 --encoder-preset slowest --quality 0 --vfr --all-audio --aencoder copy \
            --all-subtitles --subtitle-forced --format mkv --deinterlace --optimize

        [[ ! -s "$OUTPUT_FILE" ]] && echo "Failed to rip episode $EPISODE_NUM"
        ((EPISODE_NUM++))
    done

    echo "Ripping completed"
}

# Run the script
main
