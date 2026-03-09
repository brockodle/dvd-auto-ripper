#!/bin/bash

# Script to convert VC1 video to H.264 (AVC) for Plex compatibility
# Usage: ./convert_vc1_to_h264.sh input.mkv [output.mkv]
#
# Prefer AMD GPU encoding (VAAPI / h264_vaapi) when available; falls back to
# CPU (libx264) otherwise. Runs with reduced priority to avoid system overload.

INPUT_FILE="$1"
OUTPUT_FILE="${2:-${INPUT_FILE%.mkv}_h264_plex.mkv}"

if [ -z "$INPUT_FILE" ]; then
    echo "Usage: $0 input.mkv [output.mkv]"
    echo "If output filename is not specified, it will be: input_h264_plex.mkv"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: Input file '$INPUT_FILE' not found!"
    exit 1
fi

# Limit CPU threads when using software decode (and for libx264 fallback).
NPROC=$(nproc 2>/dev/null || echo 4)
THREADS=$(( NPROC > 1 ? NPROC - 1 : 1 ))
if [ "$THREADS" -gt 4 ]; then
    THREADS=4
fi

# Prefer first VAAPI (AMD/Intel) render node; skip if unset or not found.
VAAPI_DEVICE=""
for d in /dev/dri/renderD128 /dev/dri/renderD129; do
    if [ -c "$d" ]; then
        VAAPI_DEVICE="$d"
        break
    fi
done

# Prefer AMD GPU (VAAPI) encoding when available.
if [ -n "$VAAPI_DEVICE" ] && ffmpeg -hide_banner -loglevel error -vaapi_device "$VAAPI_DEVICE" -f lavfi -i "nullsrc=size=2x2" -vf "format=nv12,hwupload" -c:v h264_vaapi -frames:v 1 -f null - 2>/dev/null; then
    echo "Converting: $INPUT_FILE"
    echo "Output: $OUTPUT_FILE"
    echo ""
    echo "Using AMD GPU (VAAPI) H.264 encoding (QP 16, low CPU load)"
    echo "Audio: passthrough (copy). Subtitles: copy."
    echo "VAAPI device: $VAAPI_DEVICE"
    echo "Process priority: low (nice/ionice)"
    echo ""

    nice -n 19 ionice -c 3 ffmpeg -y -vaapi_device "$VAAPI_DEVICE" -i "$INPUT_FILE" \
        -threads "$THREADS" \
        -vf "format=nv12,hwupload" \
        -c:v h264_vaapi \
        -qp 16 \
        -max_muxing_queue_size 1024 \
        -c:a copy \
        -c:s copy \
        "$OUTPUT_FILE"
else
    echo "Converting: $INPUT_FILE"
    echo "Output: $OUTPUT_FILE"
    echo ""
    echo "Using CPU (libx264) H.264 – no VAAPI device or h264_vaapi not available"
    echo "Using CRF 16 (high quality). Audio: passthrough (copy). Threads: $THREADS"
    echo "Process priority: low (nice/ionice)"
    echo ""

    nice -n 19 ionice -c 3 ffmpeg -y -i "$INPUT_FILE" \
        -threads "$THREADS" \
        -c:v libx264 \
        -preset slow \
        -crf 16 \
        -pix_fmt yuv420p \
        -max_muxing_queue_size 1024 \
        -c:a copy \
        -c:s copy \
        "$OUTPUT_FILE"
fi

if [ $? -eq 0 ]; then
    echo ""
    echo "Conversion completed successfully!"
    echo "Output file: $OUTPUT_FILE"
else
    echo ""
    echo "Conversion failed!"
    exit 1
fi
