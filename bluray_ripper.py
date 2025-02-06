#!/usr/bin/env python3

import sys
import re
import json
import logging
from pathlib import Path
import subprocess
from datetime import datetime
import requests
from rich import print as rprint
from rich.prompt import Prompt, Confirm
from rich.progress import Progress
import shutil
import time
import os
import signal
import traceback
import tempfile
from threading import Timer

# Configuration
CONFIG = {
    'TVDB_API_KEY': '',
    'TMDB_API_KEY': '',
    'TVDB_API_URL': 'https://api4.thetvdb.com/v4',
    'TMDB_API_URL': 'https://api.themoviedb.org/3',
    'CACHE_SIZE_MB': 1024,
    'MAKEMKV_BIN': None  # Will be set during dependency check
}

# Set up logging
LOG_DIR = Path.home() / '.bluray_ripper'
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'bluray_ripper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('bluray_ripper')

# Episode duration ranges (in seconds)
DEFAULT_EPISODE_RANGES = {
    'half_with_intro': (650, 705),     # 10:50 - 11:45
    'half_no_intro': (600, 649),       # 10:00 - 10:49
    'full_episode': (1260, 1440)       # 21:00 - 24:00
}

class BluRayError(Exception):
    """Base exception for BluRay-related errors"""
    pass

def check_dependencies():
    """Verify required programs are installed"""
    try:
        logger.info("Checking for MakeMKV installation")
        
        # First check if snap is installed
        if not shutil.which('snap'):
            raise BluRayError(
                "Snap not found. Please install snapd first:\n"
                "sudo apt install snapd"
            )
        
        # Check if MakeMKV snap is installed
        result = subprocess.run(
            ['snap', 'list', 'makemkv'],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0 or 'makemkv' not in result.stdout:
            raise BluRayError(
                "MakeMKV snap not found. Please install with:\n"
                "sudo snap install makemkv"
            )
            
        # Use the correct snap path for makemkvcon
        makemkvcon_path = '/snap/bin/makemkv.makemkvcon'
        if Path(makemkvcon_path).exists():
            logger.info(f"Found MakeMKV at: {makemkvcon_path}")
            CONFIG['MAKEMKV_BIN'] = makemkvcon_path
        else:
            raise BluRayError("Could not find makemkvcon in snap installation")
        
        # Check for ffmpeg
        if not shutil.which('ffmpeg'):
            raise BluRayError("ffmpeg not found. Please install: sudo apt install ffmpeg")
            
    except Exception as e:
        raise BluRayError(f"Dependency check failed: {e}")

def find_bluray_device() -> str:
    """Find and validate BluRay device"""
    logger.info("Searching for BluRay device...")
    
    # First check mounted media locations
    mount_paths = [
        '/media',
        f'/media/{os.getenv("USER")}',
        f'/run/media/{os.getenv("USER")}',
        '/mnt'
    ]
    
    # Look for mounted BluRay disc
    for base_path in mount_paths:
        if Path(base_path).exists():
            try:
                # List all mounted volumes
                for volume in Path(base_path).glob('**/'):
                    if volume.is_dir():
                        # Try to find BDMV directory which indicates BluRay structure
                        bdmv = volume / 'BDMV'
                        if bdmv.exists() and bdmv.is_dir():
                            logger.info(f"Found mounted BluRay at: {volume}")
                            return "disc:0"  # Use disc:0 for snap makemkvcon
                            
            except Exception as e:
                logger.debug(f"Error checking {base_path}: {e}")
                continue
    
    raise BluRayError("No valid BluRay disc found")

def verify_device_access(device: str) -> bool:
    """Verify BluRay device is accessible"""
    try:
        # First check user is in cdrom group
        groups = subprocess.run(['groups'], capture_output=True, text=True).stdout.split()
        if 'cdrom' not in groups:
            logger.warning("User not in cdrom group. Some features may be limited.")
            
        cmd = [
            CONFIG['MAKEMKV_BIN'],
            '-r',  # Robot mode
            'info',
            device
        ]
        
        logger.info("Checking drive access...")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        # Even with permission warnings, check if we got disc info
        for line in result.stdout.splitlines():
            if line.startswith('TCOUNT:'):
                title_count = int(line.split(':')[1])
                logger.info(f"Found {title_count} titles on disc")
                return True
            elif line.startswith('DRV:0,'):
                parts = line.split(',')
                if len(parts) >= 6:
                    logger.info(f"Found drive: {parts[4]}, Disc: {parts[5]}")
        
        logger.error("No disc content found")
        return False
        
    except Exception as e:
        logger.error(f"Device access check failed: {e}")
        return False

def ensure_device_ready(max_retries: int = 3) -> tuple[bool, str]:
    """Ensure device is ready and mounted properly"""
    device = None
    
    for attempt in range(max_retries):
        logger.info(f"Checking device readiness (attempt {attempt + 1}/{max_retries})")
        
        try:
            # Try to find device first
            if not device:
                device = find_bluray_device()
            
            # Then verify access
            if verify_device_access(device):
                return True, device
                
        except BluRayError as e:
            logger.warning(f"Device detection failed: {e}")
        
        logger.warning(f"Device not ready on attempt {attempt + 1}, trying reset")
        try:
            # Try to reset the drive
            subprocess.run(['eject'], capture_output=True)
            time.sleep(2)
            subprocess.run(['eject', '-t'], capture_output=True)
            time.sleep(5)
            
        except Exception as e:
            logger.error(f"Device reset error: {e}")
            
        if attempt < max_retries - 1:
            time.sleep(2)
            device = None  # Reset device for next attempt
    
    return False, None

def cleanup_previous_session():
    """Clean up any leftover files and processes"""
    try:
        # Kill any running makemkvcon processes
        try:
            result = subprocess.run(
                ['pgrep', 'makemkvcon'],
                capture_output=True,
                text=True
            )
            if result.stdout:
                for pid in result.stdout.splitlines():
                    logger.info(f"Killing leftover makemkvcon process: {pid}")
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        time.sleep(1)  # Give it time to terminate
                    except ProcessLookupError:
                        pass  # Process already gone
        except Exception as e:
            logger.warning(f"Process cleanup error: {e}")

        # Check for and remove lock file
        lock_file = Path("/var/lock/bluray_ripper.lock")
        if lock_file.exists():
            try:
                with open(lock_file) as f:
                    pid = int(f.read().strip())
                try:
                    os.kill(pid, 0)  # Check if process exists
                    rprint(f"[yellow]Found running BluRay ripper process (PID: {pid})[/yellow]")
                    if Confirm.ask("Kill existing process?"):
                        os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    lock_file.unlink()
            except (ValueError, IOError):
                lock_file.unlink()

        # Clean up temporary files
        temp_dir = Path("/tmp/bluray_ripper")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            
    except Exception as e:
        logger.warning(f"Cleanup warning: {e}")

def scan_disc() -> dict:
    """Scan BluRay disc using makemkvcon and return title information"""
    rprint("[blue]Scanning disc with MakeMKV...[/blue]")
    
    try:
        cmd = [
            CONFIG['MAKEMKV_BIN'],
            '--cache=' + str(CONFIG['CACHE_SIZE_MB']),
            '--progress=-same',  # Output progress to same as messages
            '--messages=-stdout',  # Output messages to stdout
            '-r',  # Robot mode
            'info',
            'disc:0'
        ]
        
        logger.debug(f"Running scan command: {' '.join(cmd)}")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        
        titles = {}
        current_title = None
        
        for line in process.stdout:
            line = line.strip()
            
            # Parse different message types according to documentation
            if line.startswith('TINFO:'):
                # Title info format: TINFO:id,code,value
                parts = line.split(',')
                if len(parts) >= 3:
                    title_id = int(parts[0].split(':')[1])
                    code = parts[1]
                    value = parts[2]
                    
                    if title_id not in titles:
                        titles[title_id] = {
                            'number': title_id,
                            'duration': 0,
                            'size_bytes': 0,
                            'chapters': 0,
                            'name': '',
                            'filename': ''
                        }
                    
                    # Map known attribute codes
                    if code == "2":  # Name
                        titles[title_id]['name'] = value
                    elif code == "8":  # Chapters
                        titles[title_id]['chapters'] = int(value)
                    elif code == "9":  # Duration (in seconds)
                        duration_str = value.strip('"')
                        h, m, s = map(int, duration_str.split(':'))
                        titles[title_id]['duration'] = h * 3600 + m * 60 + s
                    elif code == "10":  # Size
                        size_str = value.strip('"')
                        if 'GB' in size_str:
                            gb = float(size_str.split()[0])
                            titles[title_id]['size_bytes'] = int(gb * 1024**3)
                    elif code == "27":  # Output filename
                        titles[title_id]['filename'] = value.strip('"')
            
            elif line.startswith('DRV:'):
                # Drive info format: DRV:index,visible,enabled,flags,drive name,disc name
                parts = line.split(',')
                if len(parts) >= 6:
                    logger.info(f"Found drive: {parts[4]}, Disc: {parts[5]}")
            
            elif line.startswith('TCOUT:'):
                # Title count format: TCOUT:count
                count = int(line.split(':')[1])
                logger.info(f"Found {count} titles on disc")
        
        process.wait()
        
        if process.returncode != 0:
            raise BluRayError("MakeMKV scan failed")
        
        return titles
        
    except Exception as e:
        raise BluRayError(f"Scan error: {e}")

def verify_ripped_file(file_path: Path) -> bool:
    """Verify ripped file is valid"""
    try:
        cmd = ['ffmpeg', '-v', 'error', '-i', str(file_path), '-f', 'null', '-']
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0 and file_path.stat().st_size > 1000000  # >1MB
    except Exception as e:
        logger.warning(f"Verification failed: {e}")
        return False

def handle_stalled_rip(process, start_time: float, stall_timeout: int = 300) -> bool:
    """Check for and handle stalled rips"""
    if time.time() - start_time > stall_timeout:
        rprint("[yellow]Rip appears stalled, attempting recovery...[/yellow]")
        try:
            process.kill()
            return True
        except:
            return False
    return False

def rip_title(title_num: int, output_file: Path) -> subprocess.Popen:
    """Start ripping a title using makemkvcon"""
    try:
        # Create output directory if it doesn't exist
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Clear any existing stdout file
        stdout_file = Path('stdout')
        if stdout_file.exists():
            stdout_file.unlink()
        
        cmd = [
            CONFIG['MAKEMKV_BIN'],
            '--robot',
            '--messages=stdout',
            '--progress=-same',
            'mkv',
            'disc:0',
            str(title_num),
            str(output_file.parent)
        ]
        
        logger.info(f"Ripping title {title_num} to {output_file}")
        
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
            
    except Exception as e:
        logger.error(f"Rip error: {e}")
        return None

def get_tvdb_info(show_name: str, season_num: int) -> dict:
    """Get show and season info from TVDB"""
    try:
        # First authenticate with TVDB v4
        auth_headers = {
            "accept": "application/json",
            "Content-Type": "application/json"
        }
        
        auth_data = {
            "apikey": CONFIG['TVDB_API_KEY'],
            "pin": ""
        }
        
        auth_response = requests.post(
            "https://api4.thetvdb.com/v4/login",
            headers=auth_headers,
            json=auth_data
        )
        auth_response.raise_for_status()
        auth_result = auth_response.json()
        
        if 'data' not in auth_result or 'token' not in auth_result['data']:
            raise RuntimeError("Failed to get TVDB token")
        
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {auth_result['data']['token']}"
        }
        
        # Search for show
        search_response = requests.get(
            "https://api4.thetvdb.com/v4/search",
            headers=headers,
            params={"query": show_name, "type": "series"}
        )
        search_response.raise_for_status()
        search_data = search_response.json()
        
        if 'data' not in search_data or not search_data['data']:
            raise ValueError(f"Show not found: {show_name}")
            
        show_data = search_data['data'][0]
        show_id = show_data['id']
        if isinstance(show_id, str) and show_id.startswith('series-'):
            show_id = show_id.replace('series-', '')
        
        # Get season info
        season_response = requests.get(
            f"https://api4.thetvdb.com/v4/series/{show_id}/episodes/default",
            headers=headers
        )
        season_response.raise_for_status()
        season_data = season_response.json()
        
        if 'data' not in season_data or 'episodes' not in season_data['data']:
            raise ValueError(f"Could not get episode data for season {season_num}")
            
        episodes = season_data['data']['episodes']
        season_episodes = [ep for ep in episodes if ep.get("seasonNumber") == season_num]
        
        if not season_episodes:
            raise ValueError(f"No episodes found for season {season_num}")
        
        durations = [ep.get('runtime', 0) for ep in season_episodes if ep.get('runtime', 0) > 0]
        if not durations:
            rprint("[yellow]Warning: No episode lengths found, using defaults[/yellow]")
            min_length = 20
            max_length = 60
        else:
            min_length = min(durations)
            max_length = max(durations)
        
        return {
            'show_name': show_data.get('name', show_name),
            'season_num': season_num,
            'total_episodes': len(season_episodes),
            'min_length': min_length,
            'max_length': max_length
        }
        
    except Exception as e:
        raise BluRayError(f"Failed to get show info: {e}")

def get_tmdb_info(movie_name: str) -> dict:
    """Get movie info from TMDB"""
    try:
        response = requests.get(
            f"{CONFIG['TMDB_API_URL']}/search/movie",
            params={
                'api_key': CONFIG['TMDB_API_KEY'],
                'query': movie_name
            }
        )
        response.raise_for_status()
        data = response.json()
        
        if not data.get('results'):
            raise ValueError(f"Movie not found: {movie_name}")
        
        movie = data['results'][0]
        return {
            'title': movie['title'],
            'year': movie['release_date'][:4],
            'filename': f"{movie['title']} ({movie['release_date'][:4]})"
        }
        
    except Exception as e:
        raise BluRayError(f"Failed to get movie info: {e}")

def get_show_info_from_path(path: Path) -> tuple[str, int]:
    """Extract show name and season from path"""
    try:
        # Try to get season from current folder
        season_match = re.search(r'(?:season|series)[_ ]?(\d+)', path.name, re.IGNORECASE)
        if season_match:
            season_num = int(season_match.group(1))
        else:
            season_num = None
            
        # Try to get show name from parent folder
        show_name = path.parent.name
        # Clean up common patterns in show names
        show_name = re.sub(r'\(\d{4}\)', '', show_name).strip()
        
        logger.info(f"Found show: {show_name}, Season: {season_num}")
        return show_name, season_num
        
    except Exception as e:
        logger.error(f"Could not parse path info: {e}")
        return None, None

def get_tvdb_episode_info(show_name: str, season_num: int) -> tuple[float, float]:
    """Get episode runtime range from TVDB"""
    try:
        logger.info(f"Looking up TVDB info for {show_name} Season {season_num}")
        
        # Get show ID first
        search_url = f"{CONFIG['TVDB_API_URL']}/search"
        response = requests.get(
            search_url,
            params={'query': show_name, 'type': 'series'},
            headers={"accept": "application/json", "Content-Type": "application/json"}
        )
        response.raise_for_status()
        
        shows = response.json().get('data', [])
        if not shows:
            raise BluRayError(f"Show '{show_name}' not found on TVDB")
        
        show_id = shows[0]['id']
        logger.info(f"Found show ID: {show_id}")
        
        # Get episodes for the season
        episodes_url = f"{CONFIG['TVDB_API_URL']}/series/{show_id}/episodes/official"
        response = requests.get(episodes_url, headers={"accept": "application/json", "Content-Type": "application/json"})
        response.raise_for_status()
        
        episodes = response.json().get('data', [])
        
        # Filter episodes for our season and get their runtimes
        season_runtimes = []
        for episode in episodes:
            if episode.get('seasonNumber') == season_num and episode.get('runtime'):
                season_runtimes.append(int(episode['runtime']))
        
        if not season_runtimes:
            logger.warning(f"No runtime data found for {show_name} Season {season_num}")
            # Fallback to default range of 30-60 minutes
            return 30 * 60, 60 * 60
        
        # Get min and max runtimes
        min_runtime = min(season_runtimes) * 60  # Convert to seconds
        max_runtime = max(season_runtimes) * 60
        
        logger.info(f"Found runtime range: {min_runtime/60:.1f}-{max_runtime/60:.1f} minutes")
        return min_runtime, max_runtime
        
    except requests.exceptions.RequestException as e:
        logger.error(f"TVDB API request failed: {e}")
        # Fallback to default range of 30-60 minutes
        return 30 * 60, 60 * 60
    except Exception as e:
        logger.error(f"TVDB lookup failed: {e}")
        # Fallback to default range of 30-60 minutes
        return 30 * 60, 60 * 60

def filter_episode_titles(titles: dict, min_runtime: int, max_runtime: int) -> dict:
    """Filter titles that match episode duration range"""
    return {
        num: info for num, info in titles.items()
        if min_runtime * 0.9 <= info['duration'] <= max_runtime * 1.1  # 10% margin
    }

def get_episode_ranges() -> dict:
    """Get episode duration ranges from user"""
    rprint("\n[yellow]Enter episode duration ranges (in minutes)[/yellow]")
    rprint("Press Enter to use defaults for any range, or '0' to skip that range")
    
    ranges = {}
    
    for range_name, (min_sec, max_sec) in DEFAULT_EPISODE_RANGES.items():
        min_min = min_sec / 60
        max_min = max_sec / 60
        
        rprint(f"\n{range_name} (default: {min_min:.1f}-{max_min:.1f} minutes):")
        min_input = Prompt.ask("  Min duration", default=str(min_min))
        if min_input == "0":
            continue
            
        max_input = Prompt.ask("  Max duration", default=str(max_min))
        if max_input == "0":
            continue
            
        try:
            min_duration = float(min_input) * 60  # Convert to seconds
            max_duration = float(max_input) * 60
            
            if min_duration > 0 and max_duration >= min_duration:
                ranges[range_name] = (min_duration, max_duration)
                rprint(f"[green]Added range: {min_duration/60:.1f}-{max_duration/60:.1f} minutes[/green]")
            else:
                rprint("[yellow]Invalid range, using default[/yellow]")
                ranges[range_name] = (min_sec, max_sec)
        except ValueError:
            rprint("[yellow]Invalid input, using default[/yellow]")
            ranges[range_name] = (min_sec, max_sec)
    
    if not ranges:
        rprint("[yellow]No valid ranges entered, using all defaults[/yellow]")
        return DEFAULT_EPISODE_RANGES
    
    return ranges

def find_next_available_episode(output_dir: Path, season_num: int) -> int:
    """Find the next available episode number by scanning directory"""
    # Get all existing episode files
    existing = []
    for file in output_dir.glob(f"S{season_num:02d}E*.mkv"):
        match = re.search(rf"S{season_num:02d}E(\d+)\.mkv", file.name)
        if match:
            existing.append(int(match.group(1)))
    
    if not existing:
        return 1  # No episodes yet
        
    # Find first gap or next number
    for i in range(1, max(existing) + 2):
        if i not in existing:
            return i
            
    return max(existing) + 1

def get_next_season_episode(output_dir: Path, season_num: int, 
                          max_episodes: int) -> tuple[int, int, Path]:
    """Get next available season/episode numbers and output path"""
    current_season = season_num
    current_dir = output_dir
    
    while True:
        next_episode = find_next_available_episode(current_dir, current_season)
        
        # If we've hit the episode limit for this season
        if next_episode > max_episodes:
            # Move to next season
            current_season += 1
            current_dir = output_dir.parent / f"Season {current_season}"
            current_dir.mkdir(exist_ok=True)
            next_episode = find_next_available_episode(current_dir, current_season)
        
        # Create output path
        output_file = current_dir / f"S{current_season:02d}E{next_episode:02d}.mkv"
        
        return current_season, next_episode, output_file

def sample_title(device: str, title_num: int, duration: float) -> tuple[Path, int]:
    """Create a sample from middle of title for comparison"""
    temp_dir = Path("/tmp/bluray_ripper")
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # Calculate middle point
    mid_point = duration / 2
    start_time = max(0, mid_point - 2.5)
    
    # Create sample file
    sample_file = temp_dir / f"title_{title_num}_sample.mkv"
    
    cmd = [
        CONFIG['MAKEMKV_BIN'],
        'mkv',
        device,
        str(title_num),
        str(temp_dir),
        '--noscan',  # Skip initial scan
        '--minlength=5',  # Only get 5 seconds
        f'--startat={start_time}'  # Start at calculated point
    ]
    
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=30)
        return sample_file, sample_file.stat().st_size
    except Exception as e:
        logger.error(f"Failed to create sample for title {title_num}: {e}")
        if sample_file.exists():
            sample_file.unlink()
        return None, 0

def filter_episodes(titles: dict, min_length: int, max_length: int, 
                   device: str, episode_ranges: dict) -> dict:
    """Filter titles based on episode length criteria"""
    logger.info(f"Filtering episodes (target length: {min_length}-{max_length} minutes)")
    
    episode_titles = {}
    duration_groups = {}
    
    # First pass - group by exact duration
    for num, info in titles.items():
        duration = info['duration']
        duration_min = duration / 60
        
        # Check if duration falls into any of our expected ranges
        is_valid = False
        for range_name, (min_sec, max_sec) in episode_ranges.items():
            if min_sec <= duration <= max_sec:
                is_valid = True
                logger.info(f"Title {num}: {duration_min:.2f} minutes ({range_name})")
                break
        
        if is_valid:
            if duration not in duration_groups:
                duration_groups[duration] = []
            duration_groups[duration].append((num, info))
    
    # Second pass - analyze duration groups
    for duration, group in sorted(duration_groups.items()):
        duration_min = duration / 60
        
        if len(group) >= 2:
            # Create samples for comparison
            samples = {}
            unique_titles = set()
            
            rprint(f"\n[yellow]Analyzing {len(group)} titles of {duration_min:.2f} minutes...[/yellow]")
            
            for num, info in sorted(group, key=lambda x: x[1]['chapters'], reverse=True):
                sample_file, sample_size = sample_title(device, num, duration)
                if sample_size > 0:
                    if sample_size not in samples:
                        samples[sample_size] = num
                        unique_titles.add(num)
                        for range_name, (min_sec, max_sec) in episode_ranges.items():
                            if min_sec <= duration <= max_sec:
                                rprint(f"[green]Title {num} appears to be unique "
                                      f"({range_name}, {duration_min:.2f} min)[/green]")
                                break
                    else:
                        original = samples[sample_size]
                        rprint(f"[yellow]Title {num} appears to be duplicate of title {original}[/yellow]")
                
                if sample_file and sample_file.exists():
                    sample_file.unlink()
            
            # Add unique titles
            for num in unique_titles:
                for title_num, info in group:
                    if title_num == num:
                        episode_titles[num] = info
                        break
        else:
            # Single title in group - add directly
            num, info = group[0]
            episode_titles[num] = info
            for range_name, (min_sec, max_sec) in episode_ranges.items():
                if min_sec <= duration <= max_sec:
                    rprint(f"[green]Title {num} added ({range_name}, {duration_min:.2f} min)[/green]")
                    break
    
    return episode_titles

def get_user_input() -> tuple[Path, str]:
    """Get output directory and media type from user"""
    
    # Get output location
    while True:
        output_dir = Path(Prompt.ask("[cyan]Enter output directory path[/cyan]")).expanduser()
        if not output_dir.exists():
            if Confirm.ask(f"Directory {output_dir} doesn't exist. Create it?"):
                output_dir.mkdir(parents=True)
                break
        else:
            break
    
    # Get media type
    media_type = Prompt.ask(
        "[cyan]Is this a TV Show or Movie?[/cyan]",
        choices=["TV", "MOVIE"],
        default="TV"
    )
    
    return output_dir, media_type

def get_tv_show_info(output_dir: Path) -> tuple[str, int, int]:
    """Get TV show information from user"""
    
    # Get show name
    show_name = Prompt.ask("[cyan]Enter TV Show name[/cyan]")
    
    # Try to find season number from directory
    season_match = re.search(r'season[_ ]?(\d+)', str(output_dir), re.IGNORECASE)
    if season_match:
        season_num = int(season_match.group(1))
        if Confirm.ask(f"Found Season {season_num}. Is this correct?"):
            logger.info(f"Using Season {season_num}")
        else:
            season_num = int(Prompt.ask("[cyan]Enter Season number[/cyan]"))
    else:
        season_num = int(Prompt.ask("[cyan]Enter Season number[/cyan]"))
    
    # Get starting episode
    start_episode = int(Prompt.ask("[cyan]Enter starting episode number[/cyan]"))
    
    return show_name, season_num, start_episode

def get_movie_info() -> str:
    """Get movie information from user"""
    return Prompt.ask("Enter Movie name")

def get_title_info(titles: dict) -> dict:
    """Get title info with durations and notify user of any exceptions"""
    title_info = {}
    exceptions = []
    
    for title_num, info in titles.items():
        try:
            duration = info.get('duration', 0)
            duration_mins = duration / 60
            title_info[title_num] = {
                'duration': duration,
                'duration_mins': duration_mins
            }
        except Exception as e:
            exceptions.append(f"Title {title_num}: {str(e)}")
    
    return title_info, exceptions

def filter_titles_by_duration(title_info: dict, min_runtime: float, max_runtime: float, 
                            margin_percent: float = 20) -> tuple[dict, dict]:
    """Filter titles by duration with margin and return both matching and non-matching titles"""
    margin = margin_percent / 100
    min_with_margin = min_runtime * (1 - margin)  # e.g., 20% below min
    max_with_margin = max_runtime * (1 + margin)  # e.g., 20% above max
    
    matching_titles = {}
    non_matching_titles = {}
    
    for title_num, info in title_info.items():
        duration = info['duration']
        if min_with_margin <= duration <= max_with_margin:
            matching_titles[title_num] = info
        else:
            non_matching_titles[title_num] = info
    
    return matching_titles, non_matching_titles

def cleanup_session():
    """Clean up temporary files from the session"""
    try:
        stdout_file = Path('stdout')
        if stdout_file.exists():
            stdout_file.unlink()
    except Exception as e:
        logger.debug(f"Cleanup error: {e}")

def get_next_episode_number(output_dir: Path, season_num: int) -> int:
    """Find the next episode number by checking existing files"""
    # Look for existing episode files
    pattern = f"S{season_num:02d}E*.mkv"
    existing_episodes = list(output_dir.glob(pattern))
    
    if not existing_episodes:
        return None  # No existing episodes found
    
    # Extract episode numbers from filenames
    episode_numbers = []
    for ep in existing_episodes:
        match = re.search(rf"S{season_num:02d}E(\d+)\.mkv", ep.name)
        if match:
            episode_numbers.append(int(match.group(1)))
    
    if not episode_numbers:
        return None
    
    # Return the next number after the highest existing episode
    return max(episode_numbers) + 1

def find_latest_mkv(directory: Path, created_after: float) -> Path:
    """Find the most recently created MKV file in the directory"""
    mkv_files = list(directory.glob('*.mkv'))
    recent_files = [f for f in mkv_files if f.stat().st_mtime > created_after]
    return max(recent_files, key=lambda f: f.stat().st_mtime) if recent_files else None

def process_tv_show(output_dir: Path, titles: dict):
    """Process TV show titles"""
    try:
        while True:  # Main retry loop
            try:
                # Get show info from path
                show_name, season_num = get_show_info_from_path(output_dir)
                
                # Prompt if needed
                if not show_name:
                    show_name = Prompt.ask("[cyan]Enter TV Show name[/cyan] (or 'q' to quit)")
                    if show_name.lower() == 'q':
                        return
                if not season_num:
                    season_input = Prompt.ask("[cyan]Enter Season number[/cyan] (or 'q' to quit)")
                    if season_input.lower() == 'q':
                        return
                    season_num = int(season_input)
                
                # Get title info and check for exceptions
                title_info, exceptions = get_title_info(titles)
                
                if exceptions:
                    rprint("\n[yellow]Warnings during title scanning:[/yellow]")
                    for exc in exceptions:
                        rprint(f"[yellow]  {exc}[/yellow]")
                
                # Get episode duration range from TVDB
                rprint("\n[blue]Looking up show information on TVDB...[/blue]")
                min_runtime, max_runtime = get_tvdb_episode_info(show_name, season_num)
                rprint(f"[green]Found episode runtime range: {min_runtime/60:.1f}-{max_runtime/60:.1f} minutes[/green]")
                
                # Filter titles by duration
                matching_titles, non_matching_titles = filter_titles_by_duration(title_info, min_runtime, max_runtime)
                
                # Show matching titles
                rprint("\n[cyan]Found episode titles within expected duration range:[/cyan]")
                for num, info in matching_titles.items():
                    rprint(f"[green]Title {num}:[/green] {info['duration_mins']:.1f} minutes")
                
                # Show non-matching titles and handle selection
                if non_matching_titles:
                    rprint("\n[yellow]Titles outside expected duration range:[/yellow]")
                    for num, info in non_matching_titles.items():
                        rprint(f"Title {num}: {info['duration_mins']:.1f} minutes")
                    
                    include_choice = Confirm.ask("\n[yellow]Would you like to include any of these titles?[/yellow]")
                    if include_choice:
                        additional = Prompt.ask("[cyan]Enter title numbers to include (comma-separated, or 'b' to go back)[/cyan]")
                        if additional.lower() == 'b':
                            continue
                        
                        try:
                            for num in [int(x.strip()) for x in additional.split(',')]:
                                if num in non_matching_titles:
                                    matching_titles[num] = non_matching_titles[num]
                        except ValueError:
                            rprint("[red]Invalid input. Please try again.[/red]")
                            continue
                
                if not matching_titles:
                    raise BluRayError("No valid titles found matching expected episode duration")
                
                # Check for existing episodes or prompt for starting number
                next_episode = get_next_episode_number(output_dir, season_num)
                if next_episode is not None:
                    start_episode = next_episode
                    rprint(f"\n[cyan]Continuing from episode {start_episode}[/cyan]")
                else:
                    episode_input = Prompt.ask("[cyan]Enter starting episode number[/cyan] (or 'b' to go back)")
                    if episode_input.lower() == 'b':
                        continue
                    start_episode = int(episode_input)
                
                # Show final selection and start ripping
                rprint("\n[green]Selected titles and their durations:[/green]")
                sorted_titles = sorted(matching_titles.items())
                for i, (num, info) in enumerate(sorted_titles):
                    rprint(f"Title {num}: {info['duration_mins']:.1f} minutes -> Episode {start_episode + i}")
                
                # Start ripping process
                total_rips = len(matching_titles)
                with Progress() as progress:
                    overall_task = progress.add_task("[blue]Overall progress[/blue]", total=total_rips)
                    
                    for i, (title_num, _) in enumerate(sorted_titles):
                        episode_num = start_episode + i  # Sequential episode numbering
                        output_file = output_dir / f"S{season_num:02d}E{episode_num:02d}.mkv"
                        
                        try:
                            rprint(f"\n[cyan]Ripping {i+1}/{total_rips}:[/cyan] Title {title_num} as episode {episode_num}")
                            
                            # Record timestamp before ripping
                            start_time = time.time()
                            
                            # Create progress bar for current rip
                            rip_task = progress.add_task(
                                f"[cyan]Ripping S{season_num:02d}E{episode_num:02d}[/cyan]",
                                total=100
                            )
                            
                            def update_progress():
                                try:
                                    if Path('stdout').exists():
                                        with open('stdout', 'rb') as f:
                                            try:
                                                f.seek(-1024, 2)
                                            except:
                                                f.seek(0)
                                            last_lines = f.read().decode().split('\n')
                                            
                                            # Find the last PRGV line
                                            for line in reversed(last_lines):
                                                if line.startswith('PRGV:'):
                                                    _, _, current, total = line.split(',')
                                                    # Calculate percentage
                                                    percent = (float(current) / float(total)) * 100
                                                    progress.update(rip_task, completed=percent)
                                                    break
                                except Exception as e:
                                    logger.debug(f"Progress parse error: {e}")
                                
                                if process and process.poll() is None:
                                    Timer(0.5, update_progress).start()
                            
                            process = rip_title(title_num, output_file)
                            if process:
                                Timer(0.5, update_progress).start()
                                process.wait()
                                
                                # Find and rename the newly created file
                                new_file = find_latest_mkv(output_file.parent, start_time)
                                if new_file:
                                    try:
                                        new_file.rename(output_file)
                                        rprint(f"[green]Successfully ripped episode {episode_num}[/green]")
                                    except Exception as e:
                                        logger.error(f"Rename error: {e}")
                                        rprint(f"[red]Failed to rename episode {episode_num}[/red]")
                                else:
                                    rprint(f"[red]Failed to find ripped file for episode {episode_num}[/red]")
                            else:
                                rprint(f"[red]Failed to start rip for episode {episode_num}[/red]")
                            
                            progress.update(overall_task, advance=1)
                            progress.remove_task(rip_task)
                            
                        finally:
                            cleanup_session()
                
                rprint("\n[green]You're awesome![/green]")
                break
                
            except ValueError as e:
                rprint(f"\n[red]Invalid input: {e}. Please try again.[/red]")
                continue
        
    except Exception as e:
        logger.error(f"Process error: {e}")
        cleanup_session()
        raise
    
    finally:
        cleanup_session()

def process_movie(output_dir: Path):
    """Process movie titles"""
    try:
        # Get movie info and process single title
        # ... movie processing code ...
        pass
    
    except Exception as e:
        logger.error(f"Process error: {e}")
        cleanup_session()
        raise
    
    finally:
        # Final cleanup
        cleanup_session()

def parse_title_range(range_str: str) -> list[int]:
    """Parse a range string like '1,3-5,7' into a list of numbers"""
    numbers = set()
    for part in range_str.split(','):
        if '-' in part:
            start, end = map(int, part.split('-'))
            numbers.update(range(start, end + 1))
        else:
            numbers.add(int(part))
    return sorted(list(numbers))

def parse_titles(output: str) -> dict:
    """Parse MakeMKV title information from robot mode output"""
    titles = {}
    current_title = None
    
    for line in output.splitlines():
        # Title count
        if line.startswith('TCOUNT:'):
            total_titles = int(line.split(':')[1])
            logger.debug(f"Found {total_titles} total titles")
            continue
            
        # Title info lines
        if line.startswith('TINFO:'):
            parts = line.split(',')
            if len(parts) >= 4:
                title_id = int(parts[0].split(':')[1])
                code = parts[1]
                value = ','.join(parts[3:]).strip('"')
                
                if title_id not in titles:
                    titles[title_id] = {
                        'number': title_id,
                        'duration': 0,
                        'size_bytes': 0,
                        'chapters': 0,
                        'name': '',
                        'filename': ''
                    }
                
                # Map known codes
                if code == "2":  # Name
                    titles[title_id]['name'] = value
                elif code == "8":  # Chapters
                    titles[title_id]['chapters'] = int(value)
                elif code == "9":  # Duration
                    # Convert HH:MM:SS to seconds
                    h, m, s = map(int, value.split(':'))
                    titles[title_id]['duration'] = h * 3600 + m * 60 + s
                elif code == "10":  # Size
                    # Convert "X.X GB" to bytes
                    if 'GB' in value:
                        gb = float(value.split()[0])
                        titles[title_id]['size_bytes'] = int(gb * 1024**3)
                elif code == "27":  # Output filename
                    titles[title_id]['filename'] = value
                    
        # Stream info lines (for additional metadata if needed)
        elif line.startswith('SINFO:'):
            parts = line.split(',')
            if len(parts) >= 4:
                title_id = int(parts[0].split(':')[1])
                stream_id = int(parts[1])
                code = parts[2]
                value = ','.join(parts[3:]).strip('"')
                
                # Could add stream info processing here if needed
                
    # Filter out titles shorter than 2 minutes
    filtered_titles = {
        k: v for k, v in titles.items() 
        if v['duration'] >= 120  # 2 minutes minimum
    }
    
    if filtered_titles:
        logger.info(f"Found {len(filtered_titles)} titles longer than 2 minutes")
        for num, info in filtered_titles.items():
            logger.debug(f"Title {num}: {info['duration']/60:.1f} minutes, {info['chapters']} chapters")
    else:
        logger.warning("No titles longer than 2 minutes found")
        
    return filtered_titles

def find_show_folder(base_dir: Path, show_name: str) -> list[Path]:
    """Find potential matching show folders"""
    matches = []
    try:
        # Check for exact and partial matches
        for folder in base_dir.iterdir():
            if not folder.is_dir():
                continue
            
            folder_name = folder.name.lower()
            show_name_lower = show_name.lower()
            
            # Check for exact match first
            if folder_name == show_name_lower:
                matches.insert(0, folder)  # Put exact matches first
            # Check for partial match
            elif show_name_lower in folder_name:
                matches.append(folder)
            # Check for year-tagged shows (e.g., "Show Name (2005)")
            elif re.match(rf"{re.escape(show_name_lower)}\s*\(\d{{4}}\)", folder_name):
                matches.append(folder)
                
    except Exception as e:
        logger.error(f"Error searching for show folder: {e}")
    
    return matches

def create_folder_structure(base_dir: Path, show_name: str, season_num: int) -> Path:
    """Create the standard folder structure"""
    show_dir = base_dir / show_name
    season_dir = show_dir / f"Season {season_num}"
    
    show_dir.mkdir(exist_ok=True)
    season_dir.mkdir(exist_ok=True)
    
    return season_dir

def setup_output_directory(provided_dir: Path) -> tuple[Path, str, int]:
    """Set up the output directory structure and return the final path"""
    try:
        # First check if we're already in a season folder
        season_match = re.search(r'(?:season|series)[_ ]?(\d+)', provided_dir.name, re.IGNORECASE)
        parent_is_show = False
        
        if season_match:
            season_num = int(season_match.group(1))
            show_name = provided_dir.parent.name
            # Clean up show name (remove year tags etc)
            show_name = re.sub(r'\s*\(\d{4}\)', '', show_name).strip()
            parent_is_show = True
            return provided_dir, show_name, season_num
        
        # If not in season folder, prompt for show and season
        show_name = Prompt.ask("[cyan]Enter TV Show name[/cyan]")
        season_num = int(Prompt.ask("[cyan]Enter Season number[/cyan]"))
        
        # Look for matching show folders
        potential_shows = find_show_folder(provided_dir, show_name)
        
        if potential_shows:
            rprint("\n[yellow]Found potential matching show folders:[/yellow]")
            for i, folder in enumerate(potential_shows, 1):
                rprint(f"[green]{i}:[/green] {folder.name}")
            rprint(f"[green]{len(potential_shows) + 1}:[/green] Create new folder")
            
            choice = Prompt.ask(
                "\n[cyan]Choose a folder number or press Enter to create new[/cyan]",
                default=str(len(potential_shows) + 1)
            )
            
            if int(choice) <= len(potential_shows):
                show_dir = potential_shows[int(choice) - 1]
                # Check for existing season folder
                season_dir = show_dir / f"Season {season_num}"
                if not season_dir.exists():
                    rprint(f"\n[yellow]Creating season folder: {season_dir.name}[/yellow]")
                    season_dir.mkdir()
                return season_dir, show_dir.name, season_num
        
        # Create new folder structure
        rprint(f"\n[yellow]Creating new folder structure for '{show_name}'[/yellow]")
        season_dir = create_folder_structure(provided_dir, show_name, season_num)
        return season_dir, show_name, season_num
        
    except Exception as e:
        logger.error(f"Error setting up directory structure: {e}")
        raise BluRayError(f"Failed to set up directory structure: {e}")

def main():
    """Main entry point"""
    try:
        logger.info("Starting BluRay ripper")
        
        # Clean up from previous runs
        cleanup_session()
        
        # Get output directory
        output_dir = Path(Prompt.ask("[cyan]Enter output directory path[/cyan]")).expanduser()
        
        # Set up proper directory structure
        output_dir, show_name, season_num = setup_output_directory(output_dir)
        
        # Check for MakeMKV
        rprint("[blue]Checking dependencies...[/blue]")
        check_dependencies()
        
        # Find and verify device
        rprint("[blue]Looking for BluRay drive...[/blue]")
        ready, device = ensure_device_ready()
        if not ready or not device:
            raise BluRayError("Could not access BluRay drive")
            
        # Store found device in config
        CONFIG['BLURAY_DEVICE'] = device
        
        # Get initial disc info and titles
        rprint("[blue]Scanning disc...[/blue]")
        cmd = [
            CONFIG['MAKEMKV_BIN'],
            '-r',
            'info',
            device
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise BluRayError("Failed to get disc info")
            
        # Parse titles from output
        titles = parse_titles(result.stdout)
        if not titles:
            raise BluRayError("No valid titles found on disc")
            
        # Process based on media type
        if show_name:
            process_tv_show(output_dir, titles)
        else:
            process_movie(output_dir)
            
        # Show completion message
        rprint("\n[green]Ripping complete![/green]")
        rprint("[blue]You're awesome![/blue]")
            
    except KeyboardInterrupt:
        logger.warning("Process interrupted by user")
        rprint("\n[yellow]Process interrupted by user[/yellow]")
        cleanup_session()
        sys.exit(0)
    except BluRayError as e:
        logger.error(f"Setup error: {e}")
        rprint(f"[red]Setup error: {e}[/red]")
        if Confirm.ask("[yellow]Try again?[/yellow]"):
            main()
        cleanup_session()
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error")
        rprint(f"[red]Unexpected error: {e}[/red]")
        cleanup_session()
        sys.exit(1)
    finally:
        cleanup_session()

if __name__ == '__main__':
    main()
