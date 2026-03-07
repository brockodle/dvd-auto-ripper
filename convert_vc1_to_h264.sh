#!/bin/bash

# Script to convert VC1 video to H.264 (AVC) for Plex compatibility
# Usage: ./convert_vc1_to_h264.sh input.mkv [output.mkv]

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

echo "Converting: $INPUT_FILE"
echo "Output: $OUTPUT_FILE"
echo ""
echo "Using H.264 with CRF 12 (very high quality, near-lossless)"
echo "This will preserve all audio and subtitle tracks"
echo ""

ffmpeg -y -i "$INPUT_FILE" \
    -c:v libx264 \
    -preset slow \
    -crf 12 \
    -pix_fmt yuv420p \
    -c:a copy \
    -c:s copy \
    "$OUTPUT_FILE"

if [ $? -eq 0 ]; then
    echo ""
    echo "Conversion completed successfully!"
    echo "Output file: $OUTPUT_FILE"
else
    echo ""
    echo "Conversion failed!"
    exit 1
fi
