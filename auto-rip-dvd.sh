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
# Configuration and Utility Functions
###################
handle_error() {
    echo "Error: $1" >&2
    exit 1
}

log_api_response() {
    local api_name="$1"
    local response="$2"
    local timestamp=$(date +%Y%m%d_%H%M%S)
    local log_file="JSON outputs/${timestamp}_${api_name}.json"
    
    mkdir -p "JSON outputs"
    echo "$response" > "$log_file"
    echo "API response logged to $log_file"
}

ensure_dependencies() {
    local required_commands=("HandBrakeCLI" "curl" "jq" "sed" "dd")
    for cmd in "${required_commands[@]}"; do
        command -v "$cmd" &> /dev/null || handle_error "Required command '$cmd' not found"
    done
}

###################
# Blu-ray Support Functions
###################
check_bluray_support() {
    local required_packages=("libaacs0" "libbluray2" "libbluray-bdj")
    local missing_packages=()

    echo "Checking Blu-ray support..."
    
    # Check for AACS config directory
    if [[ ! -d "$HOME/.config/aacs" ]]; then
        mkdir -p "$HOME/.config/aacs"
    fi

    # Check for KEYDB.cfg
    if [[ ! -f "$HOME/.config/aacs/KEYDB.cfg" ]]; then
        echo "Warning: AACS KEYDB.cfg not found. Blu-ray playback may be limited."
        echo "You may need to obtain KEYDB.cfg for full Blu-ray support."
    fi

    # Check for required packages
    for pkg in "${required_packages[@]}"; do
        if ! dpkg -l | grep -q "^ii  $pkg "; then
            missing_packages+=("$pkg")
        fi
    done

    if [[ ${#missing_packages[@]} -gt 0 ]]; then
        echo "Missing required packages for Blu-ray support:"
        printf '%s\n' "${missing_packages[@]}"
        read -rp "Would you like to install them now? (y/n): " choice
        if [[ "${choice,,}" == "y" ]]; then
            sudo apt-get update
            sudo apt-get install -y "${missing_packages[@]}"
        else
            echo "Warning: Blu-ray support may be limited without these packages."
        fi
    fi
}

get_disc_type() {
    if blkid "$DVD_DEVICE" | grep -qi "udf"; then
        echo "Detecting disc type..."
        local disc_info
        disc_info=$(blkid "$DVD_DEVICE")
        
        if echo "$disc_info" | grep -qi "blu-ray"; then
            echo "bluray"
        else
            echo "dvd"
        fi
    else
        echo "dvd"
    fi
}

ensure_disc_readable() {
    [[ ! -b "$DVD_DEVICE" ]] && handle_error "Disc device $DVD_DEVICE not found"
    
    # Try to read the disc
    if ! dd if="$DVD_DEVICE" of=/dev/null count=1 bs=2048 2>/dev/null; then
        handle_error "Cannot read disc device $DVD_DEVICE. Check if a disc is inserted."
    fi

    # Check disc type
    DISC_TYPE=$(get_disc_type)
    if [[ "$DISC_TYPE" == "bluray" ]]; then
        echo "Detected Blu-ray disc"
        check_bluray_support
    else
        echo "Detected DVD disc"
    fi
}

###################
# DVD Functions
###################
scan_disc() {
    mkdir -p "$LOG_DIR"
    echo "Scanning disc..."
    
    # Check if it's a Blu-ray disc
    if blkid "$DVD_DEVICE" | grep -qi "udf"; then
        check_bluray_support
    fi
    
    # Try scanning with increased verbosity for debugging
    if ! HandBrakeCLI -i "$DVD_DEVICE" -t 0 --scan --min-duration 1 -v 2>&1 | tee "$SCAN_LOG"; then
        # If scan fails, try with alternative options
        if ! HandBrakeCLI -i "$DVD_DEVICE" -t 1 --scan --min-duration 1 -v 2>&1 | tee "$SCAN_LOG"; then
            handle_error "Failed to scan disc. This might be due to encryption or disc format."
        fi
    fi

    if ! grep -q "scan: DVD has" "$SCAN_LOG" && ! grep -q "scan: BD has" "$SCAN_LOG"; then
        handle_error "Failed to detect disc structure."
    fi
    echo "Disc scan complete"
}

get_media_type() {
    while true; do
        read -rp "Is this a TV Show or Movie? (T/M): " media_type
        case "${media_type,,}" in
            t|tv|show) echo "show"; return ;;
            m|movie) echo "movie"; return ;;
            *) echo "Please enter T for TV Show or M for Movie" ;;
        esac
    done
}

get_rip_mode() {
    while true; do
        read -rp "Is this a compilation disc or single episodes? (C/S): " rip_mode
        case "${rip_mode,,}" in
            c|compilation) echo "compilation"; return ;;
            s|single) echo "single"; return ;;
            *) echo "Please enter C for Compilation or S for Single episodes" ;;
        esac
    done
}

get_duration_threshold() {
    local mode="$1"
    local default_compilation=90  # 1.5 hours in minutes
    local default_episode=60      # 1 hour in minutes
    
    if [[ "$mode" == "compilation" ]]; then
        while true; do
            read -rp "Enter minimum length for compilation titles in minutes (default ${default_compilation}): " input
            # If empty, use default
            if [[ -z "$input" ]]; then
                echo $((default_compilation * 60))  # Convert to seconds
                return
            fi
            # Validate input is a positive number
            if [[ "$input" =~ ^[0-9]+$ ]] && ((input > 0)); then
                echo $((input * 60))  # Convert to seconds
                return
            fi
            echo "Please enter a valid positive number"
        done
    else
        while true; do
            read -rp "Enter maximum length for single episodes in minutes (default ${default_episode}): " input
            # If empty, use default
            if [[ -z "$input" ]]; then
                echo $((default_episode * 60))  # Convert to seconds
                return
            fi
            # Validate input is a positive number
            if [[ "$input" =~ ^[0-9]+$ ]] && ((input > 0)); then
                echo $((input * 60))  # Convert to seconds
                return
            fi
            echo "Please enter a valid positive number"
        done
    fi
}

find_titles() {
    local mode="$1"  # "compilation" or "single"
    local titles=()
    local chapters=()
    local current_title=""
    local current_chapters=0
    local duration_seconds=0
    
    # Get only the relevant threshold based on mode
    local compilation_threshold=5400  # Default 1.5 hours in seconds
    local episode_threshold=3600      # Default 1 hour in seconds
    
    if [[ "$mode" == "compilation" ]]; then
        compilation_threshold=$(get_duration_threshold "compilation")
        echo "Debug: Using compilation threshold: $((compilation_threshold/60)) minutes" >&2
    else
        episode_threshold=$(get_duration_threshold "single")
        echo "Debug: Using episode threshold: $((episode_threshold/60)) minutes" >&2
    fi

    while IFS= read -r line; do
        # Debug output
        if [[ $line =~ duration: ]] || [[ $line =~ chapters: ]] || [[ $line =~ ^[[:space:]]*\+[[:space:]]*title ]]; then
            echo "Parsing: $line" >&2
        fi

        # Detect title line and process previous title
        if [[ $line =~ ^[[:space:]]*\+[[:space:]]*title[[:space:]]+([0-9]+): ]]; then
            if [[ -n $current_title && $duration_seconds -gt 0 ]]; then
                if [[ "$mode" == "compilation" && $duration_seconds -ge $compilation_threshold ]] || \
                   [[ "$mode" == "single" && $duration_seconds -gt 0 && $duration_seconds -lt $episode_threshold ]]; then
                    titles+=("$current_title")
                    chapters+=("$current_chapters")
                fi
            fi
            current_title="${BASH_REMATCH[1]}"
            current_chapters=0
            duration_seconds=0
        fi

        # Parse duration line
        if [[ $line =~ ^[[:space:]]*\+[[:space:]]*duration:[[:space:]]+([0-9]{2}):([0-9]{2}):([0-9]{2}) ]] && [[ -n $current_title ]]; then
            local hours=$((10#${BASH_REMATCH[1]}))
            local minutes=$((10#${BASH_REMATCH[2]}))
            local seconds=$((10#${BASH_REMATCH[3]}))
            duration_seconds=$((hours * 3600 + minutes * 60 + seconds))
            echo "Debug: Title $current_title duration: ${hours}h:${minutes}m:${seconds}s ($duration_seconds seconds)" >&2
        fi

        # Count chapters
        if [[ $line =~ ^[[:space:]]*\+[[:space:]]+([0-9]+):[[:space:]]+duration ]]; then
            ((current_chapters++))
        fi
    done < "$SCAN_LOG"

    # Process the last title
    if [[ -n $current_title && $duration_seconds -gt 0 ]]; then
        if [[ "$mode" == "compilation" && $duration_seconds -ge $compilation_threshold ]] || \
           [[ "$mode" == "single" && $duration_seconds -gt 0 && $duration_seconds -lt $episode_threshold ]]; then
            titles+=("$current_title")
            chapters+=("$current_chapters")
        fi
    fi

    if [[ ${#titles[@]} -eq 0 ]]; then
        if [[ "$mode" == "compilation" ]]; then
            handle_error "No titles found with duration >= $((compilation_threshold/60)) minutes"
        else
            handle_error "No titles found with duration < $((episode_threshold/60)) minutes"
        fi
    fi

    # Print found titles and their chapters for debugging
    if [[ "$mode" == "compilation" ]]; then
        echo "Found titles over $((compilation_threshold/60)) minutes:" >&2
    else
        echo "Found titles under $((episode_threshold/60)) minutes:" >&2
    fi
    for ((i=0; i<${#titles[@]}; i++)); do
        echo "Title ${titles[i]}: ${chapters[i]} chapters" >&2
    done

    local titles_str=$(IFS=,; echo "${titles[*]}")
    local chapters_str=$(IFS=,; echo "${chapters[*]}")
    echo "${titles_str}:${chapters_str}"
}

###################
# Ripping Functions
###################
rip_episode() {
    local dvd_device="$1"
    local title="$2"
    local chapter="$3"
    local output_file="$4"

    echo "Attempting to rip using NVENC..."
    if HandBrakeCLI -i "$dvd_device" \
        -o "$output_file" \
        --title "$title" \
        --chapters "$chapter" \
        --encoder nvenc_h265 \
        --encoder-preset slowest \
        --quality 0 \
        --vfr \
        --all-audio \
        --aencoder copy \
        --all-subtitles \
        --subtitle-forced \
        --format mkv \
        --deinterlace \
        --optimize; then
        
        if [ -s "$output_file" ]; then
            echo "NVENC encoding successful"
            return 0
        fi
    fi

    echo "Falling back to CPU encoding..."
    HandBrakeCLI -i "$dvd_device" \
        -o "$output_file" \
        --title "$title" \
        --chapters "$chapter" \
        --encoder x265 \
        --encoder-preset slowest \
        --quality 0 \
        --vfr \
        --all-audio \
        --aencoder copy \
        --all-subtitles \
        --subtitle-forced \
        --format mkv \
        --deinterlace \
        --optimize
    
    [ ! -s "$output_file" ] && return 1
    return 0
}

###################
# Main Processing Functions
###################
process_tv_show() {
    local output_dir="$1"
    
    # Step 1: Get show name
    read -rp "Enter show name: " SHOW_NAME
    
    # Step 2: Detect or get season number
    local SEASON_NUM
    if [[ "$(basename "$output_dir")" =~ ^[Ss]eason[_\ ]?([0-9]+)$ ]]; then
        SEASON_NUM="${BASH_REMATCH[1]}"
        echo "Detected Season $SEASON_NUM from folder name"
    else
        read -rp "Enter season number: " SEASON_NUM
        [[ ! "$SEASON_NUM" =~ ^[0-9]+$ ]] && handle_error "Invalid season number"
    fi
    
    # Step 3: Get starting episode number
    read -rp "Enter starting episode number: " STARTING_EPISODE
    [[ ! "$STARTING_EPISODE" =~ ^[0-9]+$ ]] && handle_error "Invalid episode number"
    
    # Step 4-7: Get show info and episode lengths from TVDB
    echo "Authenticating with TVDB..."
    TVDB_TOKEN=$(authenticate_tvdb)
    
    echo "Fetching show information..."
    SHOW_ID=$(get_show_info "$SHOW_NAME" "$TVDB_TOKEN")
    
    echo "Fetching season information..."
    IFS=: read -r _ TOTAL_EPISODES MIN_LENGTH MAX_LENGTH < <(get_season_info "$SHOW_ID" "$SEASON_NUM" "$TVDB_TOKEN")
    echo "Season $SEASON_NUM has $TOTAL_EPISODES episodes (lengths: $MIN_LENGTH-$MAX_LENGTH minutes)"
    
    # Step 5: Get rip mode
    local RIP_MODE
    while true; do
        read -rp "Rip as compilation or by-the-episode? (C/E): " choice
        case "${choice,,}" in
            c|compilation) RIP_MODE="compilation"; break ;;
            e|episode) RIP_MODE="episode"; break ;;
            *) echo "Please enter C for compilation or E for by-the-episode" ;;
        esac
    done
    
    # Scan DVD
    echo "Scanning DVD structure..."
    scan_disc
    
    # Steps 8-9: Process based on mode
    if [[ "$RIP_MODE" == "episode" ]]; then
        echo "Finding titles matching episode length criteria..."
        IFS=: read -r TITLES_STR CHAPTERS_STR < <(find_titles_by_criteria "episode" "$MIN_LENGTH" "$MAX_LENGTH")
    else
        echo "Finding compilation titles..."
        IFS=: read -r TITLES_STR CHAPTERS_STR < <(find_titles_by_criteria "compilation" 90 0)
    fi
    
    IFS=, read -ra TITLES <<< "$TITLES_STR"
    IFS=, read -ra CHAPTER_COUNTS <<< "$CHAPTERS_STR"
    
    # Calculate total chapters to process
    local total_chapters=0
    for count in "${CHAPTER_COUNTS[@]}"; do
        ((total_chapters += count))
    done
    
    echo "Found ${#TITLES[@]} titles with $total_chapters total chapters"
    echo "This will rip episodes $STARTING_EPISODE through $((STARTING_EPISODE + total_chapters - 1))"
    read -rp "Continue? (y/n): " confirm
    [[ ! "$confirm" =~ ^[Yy] ]] && handle_error "Operation cancelled by user"
    
    # Check if we'll overflow into next season
    if ((STARTING_EPISODE + total_chapters - 1 > TOTAL_EPISODES)); then
        echo "Warning: Ripping will continue into next season"
        local parent_dir="$(dirname "$output_dir")"
        local next_season=$((SEASON_NUM + 1))
        mkdir -p "${parent_dir}/Season_$(printf "%02d" "$next_season")"
    fi
    
    # Process titles
    EPISODE_NUM=$STARTING_EPISODE
    CURRENT_SEASON=$SEASON_NUM
    echo "Starting rip process..."
    
    for ((i=0; i<${#TITLES[@]}; i++)); do
        TITLE="${TITLES[i]}"
        CHAPTER_COUNT="${CHAPTER_COUNTS[i]}"
        
        if [[ "$RIP_MODE" == "episode" ]]; then
            # Rip entire title as one episode
            OUTPUT_FILE="$output_dir/S$(printf "%02d" "$CURRENT_SEASON")E$(printf "%02d" "$EPISODE_NUM").mkv"
            
            if [[ -f "$OUTPUT_FILE" ]]; then
                OUTPUT_FILE=$(handle_duplicate_file "$OUTPUT_FILE")
            fi
            
            if rip_episode "$DVD_DEVICE" "$TITLE" "1-$CHAPTER_COUNT" "$OUTPUT_FILE"; then
                ((EPISODE_NUM++))
            fi
        else
            # Rip each chapter as separate episode
            for ((CHAPTER=1; CHAPTER<=CHAPTER_COUNT; CHAPTER++)); do
                if ((EPISODE_NUM > TOTAL_EPISODES)); then
                    CURRENT_SEASON=$((CURRENT_SEASON + 1))
                    EPISODE_NUM=1
                    TOTAL_EPISODES=$(get_season_info "$SHOW_ID" "$CURRENT_SEASON" "$TVDB_TOKEN" | cut -d: -f2)
                    OUTPUT_DIR="$(dirname "$output_dir")/Season_$(printf "%02d" "$CURRENT_SEASON")"
                    mkdir -p "$OUTPUT_DIR"
                fi
                
                OUTPUT_FILE="$OUTPUT_DIR/S$(printf "%02d" "$CURRENT_SEASON")E$(printf "%02d" "$EPISODE_NUM").mkv"
                
                if [[ -f "$OUTPUT_FILE" ]]; then
                    OUTPUT_FILE=$(handle_duplicate_file "$OUTPUT_FILE")
                fi
                
                if rip_episode "$DVD_DEVICE" "$TITLE" "$CHAPTER" "$OUTPUT_FILE"; then
                    ((EPISODE_NUM++))
                fi
            done
        fi
    done
    
    echo "Ripping complete!"
    echo "You're awesome! 🎉"
}

process_movie() {
    local output_dir="$1"
    
    # Step 1: Get movie name
    read -rp "Enter movie name: " MOVIE_NAME
    
    # Step 2: Get movie info from TMDB
    echo "Looking up movie information..."
    local movie_filename
    movie_filename=$(get_movie_info "$MOVIE_NAME")
    
    # Steps 3-4: Scan disc and find best title
    echo "Scanning DVD..."
    scan_disc
    
    echo "Finding title with most chapters..."
    IFS=: read -r TITLES_STR CHAPTERS_STR < <(find_titles_by_criteria "movie" 0 0)
    IFS=, read -ra TITLES <<< "$TITLES_STR"
    
    # Use first title found (should be the one with most chapters)
    TITLE="${TITLES[0]}"
    
    # Step 5: Rip the movie
    OUTPUT_FILE="$output_dir/${movie_filename}.mkv"
    
    if [[ -f "$OUTPUT_FILE" ]]; then
        OUTPUT_FILE=$(handle_duplicate_file "$OUTPUT_FILE")
    fi
    
    if rip_episode "$DVD_DEVICE" "$TITLE" "1" "$OUTPUT_FILE"; then
        echo "Successfully ripped movie!"
        echo "You're awesome! 🎉"
    else
        handle_error "Failed to rip movie"
    fi
}

###################
# Main Script
###################
main() {
    ensure_dependencies
    ensure_disc_readable
    
    # Step 1: Get output location
    read -rp "Enter output directory (or press Enter to use current directory): " OUTPUT_DIR
    if [[ -z "$OUTPUT_DIR" ]]; then
        OUTPUT_DIR="$(pwd)"
    else
        OUTPUT_DIR="${OUTPUT_DIR%/}"
        OUTPUT_DIR="${OUTPUT_DIR/#\~/$HOME}"
    fi
    mkdir -p "$OUTPUT_DIR" || handle_error "Cannot create output directory"
    
    # Step 2: Get media type
    local MEDIA_TYPE
    while true; do
        read -rp "Is this a TV Show or Movie? (T/M): " choice
        case "${choice,,}" in
            t|tv|show) MEDIA_TYPE="show"; break ;;
            m|movie) MEDIA_TYPE="movie"; break ;;
            *) echo "Please enter T for TV Show or M for Movie" ;;
        esac
    done
    
    # Process based on media type
    if [[ "$MEDIA_TYPE" == "show" ]]; then
        process_tv_show "$OUTPUT_DIR"
    else
        process_movie "$OUTPUT_DIR"
    fi
    
    echo
    read -rp "Insert next disc and press Enter to continue, or type 'exit' to quit: " response
    [[ "${response,,}" == "exit" ]] && break
}

# Run the script
main

get_unique_filename() {
    local base_file="$1"
    local extension="${base_file##*.}"
    local filename="${base_file%.*}"
    local counter=1
    local new_filename="$base_file"

    while [[ -f "$new_filename" ]]; do
        new_filename="${filename}_${counter}.${extension}"
        ((counter++))
    done
    echo "$new_filename"
}

handle_duplicate_file() {
    local original_file="$1"
    local parent_dir="$(dirname "$(dirname "$original_file")")"
    local out_of_order_dir="${parent_dir}/Out of Order"
    local filename="$(basename "$original_file")"
    
    mkdir -p "$out_of_order_dir"
    echo "${out_of_order_dir}/${filename}"
}

###################
# API Functions
###################
authenticate_tvdb() {
    local auth_response
    auth_response=$(curl -s -X POST "${TVDB_API_URL}/login" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -d "{\"apikey\":\"${TVDB_API_KEY}\"}")
    
    log_api_response "auth" "$auth_response"
    
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
    search_response=$(curl -s -X GET "$search_url" \
        -H "Authorization: Bearer $token" \
        -H "Accept: application/json")
    
    log_api_response "search" "$search_response"

    local show_id
    show_id=$(echo "$search_response" | jq -r '.data[0].id // empty')
    [[ -z "$show_id" ]] && handle_error "Show not found"

    echo "$show_id"
}

get_season_info() {
    local show_id="$1"
    local season_num="$2"
    local token="$3"

    local series_url="${TVDB_API_URL}/series/${show_id}/extended"
    local series_response
    series_response=$(curl -s -X GET "$series_url" \
        -H "Authorization: Bearer $token" \
        -H "Accept: application/json")
    
    log_api_response "series" "$series_response"

    # Get episode count and length information
    local episode_data
    episode_data=$(echo "$series_response" | jq -r --arg season "$season_num" \
        '.data.episodes | map(select(.seasonNumber == ($season|tonumber)))')
    
    local total_episodes
    total_episodes=$(echo "$episode_data" | jq -r 'length')
    [[ $total_episodes -eq 0 ]] && handle_error "No episodes found for season $season_num"

    # Get min and max episode lengths
    local min_length
    local max_length
    min_length=$(echo "$episode_data" | jq -r 'map(.runtime) | min')
    max_length=$(echo "$episode_data" | jq -r 'map(.runtime) | max')

    echo "$total_episodes:$min_length:$max_length"
}

get_movie_info() {
    local movie_name="$1"
    
    local search_url="${TMDB_API_URL}/search/movie?api_key=${TMDB_API_KEY}&query=$(echo "$movie_name" | jq -sRr @uri)"
    local search_response
    search_response=$(curl -s -X GET "$search_url")
    
    log_api_response "movie_search" "$search_response"
    
    local movie_info
    movie_info=$(echo "$search_response" | jq -r '.results[0] | "\(.title) (\(.release_date[:4]))"')
    [[ -z "$movie_info" || "$movie_info" == " ()" ]] && handle_error "Movie not found"
    
    echo "$movie_info"
}

###################
# DVD Analysis Functions
###################
find_titles_by_criteria() {
    local mode="$1"  # "compilation", "episode", or "movie"
    local min_length="$2"  # in minutes
    local max_length="$3"  # in minutes
    local titles=()
    local chapters=()
    local current_title=""
    local current_chapters=0
    local duration_seconds=0

    while IFS= read -r line; do
        # Detect title line and process previous title
        if [[ $line =~ ^[[:space:]]*\+[[:space:]]*title[[:space:]]+([0-9]+): ]]; then
            if [[ -n $current_title && $duration_seconds -gt 0 ]]; then
                case "$mode" in
                    "compilation")
                        if ((duration_seconds >= 5400)); then  # > 1.5 hours
                            titles+=("$current_title")
                            chapters+=("$current_chapters")
                        fi
                        ;;
                    "episode")
                        if ((duration_seconds >= min_length*60 && duration_seconds <= max_length*60)); then
                            titles+=("$current_title")
                            chapters+=("$current_chapters")
                        fi
                        ;;
                    "movie")
                        if ((current_chapters > 0)); then
                            titles+=("$current_title")
                            chapters+=("$current_chapters")
                        fi
                        ;;
                esac
            fi
            current_title="${BASH_REMATCH[1]}"
            current_chapters=0
            duration_seconds=0
        fi

        # Parse duration line
        if [[ $line =~ ^[[:space:]]*\+[[:space:]]*duration:[[:space:]]+([0-9]{2}):([0-9]{2}):([0-9]{2}) ]] && [[ -n $current_title ]]; then
            local hours=$((10#${BASH_REMATCH[1]}))
            local minutes=$((10#${BASH_REMATCH[2]}))
            local seconds=$((10#${BASH_REMATCH[3]}))
            duration_seconds=$((hours * 3600 + minutes * 60 + seconds))
            echo "Debug: Title $current_title duration: ${hours}h:${minutes}m:${seconds}s ($duration_seconds seconds)" >&2
        fi

        # Count chapters
        if [[ $line =~ ^[[:space:]]*\+[[:space:]]+([0-9]+):[[:space:]]+duration ]]; then
            ((current_chapters++))
        fi
    done < "$SCAN_LOG"

    # Process the last title
    if [[ -n $current_title && $duration_seconds -gt 0 ]]; then
        case "$mode" in
            "compilation")
                if ((duration_seconds >= 5400)); then
                    titles+=("$current_title")
                    chapters+=("$current_chapters")
                fi
                ;;
            "episode")
                if ((duration_seconds >= min_length*60 && duration_seconds <= max_length*60)); then
                    titles+=("$current_title")
                    chapters+=("$current_chapters")
                fi
                ;;
            "movie")
                if ((current_chapters > 0)); then
                    titles+=("$current_title")
                    chapters+=("$current_chapters")
                fi
                ;;
        esac
    fi

    if [[ ${#titles[@]} -eq 0 ]]; then
        handle_error "No suitable titles found for $mode mode"
    fi

    local titles_str=$(IFS=,; echo "${titles[*]}")
    local chapters_str=$(IFS=,; echo "${chapters[*]}")
    echo "${titles_str}:${chapters_str}"
}
