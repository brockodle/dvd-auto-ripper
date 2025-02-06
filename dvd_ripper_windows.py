#!/usr/bin/env python3

import sys
import subprocess
from pathlib import Path

def check_and_install_requirements():
    """Check and install required Python packages"""
    required_packages = {
        'pywin32': 'win32api',
        'psutil': 'psutil',
        'rich': 'rich',
        'requests': 'requests'
    }
    
    missing = []
    for package, import_name in required_packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(package)
    
    if missing:
        print("Missing required packages:", ", ".join(missing))
        install = input("Would you like to install them now? (y/n): ").lower()
        if install == 'y':
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)
                print("\nPackages installed successfully!")
                print("Please restart the script.\n")
                sys.exit(0)
            except subprocess.CalledProcessError as e:
                print(f"\nError installing packages: {e}")
                print("Please install them manually using:")
                print(f"pip install {' '.join(missing)}")
                sys.exit(1)
        else:
            print("\nPlease install the required packages manually using:")
            print(f"pip install {' '.join(missing)}")
            sys.exit(1)

# Check requirements before importing anything else
check_and_install_requirements()

# Now import everything else
import re
import json
import logging
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

# Configuration
CONFIG = {
    'TVDB_API_KEY': '',
    'TMDB_API_KEY': '',
    'TVDB_API_URL': 'https://api4.thetvdb.com/v4',
    'TMDB_API_URL': 'https://api.themoviedb.org/3',
    'DVD_DEVICE': 'D:'  # Default DVD drive letter for Windows
}

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(Path.home() / '.dvd_ripper' / 'dvd_ripper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('dvd_ripper')

# Change constant to defaults
DEFAULT_EPISODE_RANGES = {
    'half_with_intro': (650, 705),     # 10:50 - 11:45
    'half_no_intro': (600, 649),       # 10:00 - 10:49
    'full_episode': (1260, 1440)       # 21:00 - 24:00
}

class DVDError(Exception):
    """Base exception for DVD-related errors"""
    pass

def find_dvd_device() -> Path:
    """Find and validate DVD device on Windows"""
    import win32api
    import win32file
    
    drives = win32api.GetLogicalDriveStrings().split('\000')[:-1]
    for drive in drives:
        try:
            drive_type = win32file.GetDriveType(drive)
            if drive_type == win32file.DRIVE_CDROM:
                device = Path(drive)
                # Test if drive contains media
                if verify_device_access(device):
                    return device
        except Exception as e:
            logger.warning(f"Error checking drive {drive}: {e}")
            continue
    
    raise DVDError("No valid DVD device found")

def check_dependencies():
    """Verify required programs are installed"""
    required = ['HandBrakeCLI', 'dd']
    missing = [cmd for cmd in required if not shutil.which(cmd)]
    
    if missing:
        raise DVDError(f"Missing required programs: {', '.join(missing)}")

def verify_device_access(device: Path) -> bool:
    """Verify device is accessible and contains a DVD on Windows"""
    import win32api
    import win32file
    import winerror
    
    try:
        # Check if drive exists
        if not device.exists():
            logger.error(f"Device {device} does not exist")
            return False
        
        # Check if media is present
        try:
            win32api.GetVolumeInformation(str(device))
            return True
        except win32api.error as e:
            if e.winerror == winerror.ERROR_NOT_READY:
                logger.error("Drive not ready or no disc present")
            return False
            
    except Exception as e:
        logger.error(f"Device access check failed: {e}")
        return False

def ensure_device_ready(device: Path, max_retries: int = 3) -> bool:
    """Ensure device is ready on Windows"""
    import ctypes
    import win32api
    
    for attempt in range(max_retries):
        if verify_device_access(device):
            if verify_handbrake_can_read(device):
                return True
                
        logger.warning(f"Device not ready on attempt {attempt + 1}, trying reset")
        try:
            # Eject/close drive using Windows API
            ctypes.windll.WINMM.mciSendStringW(f"open {device} type cdaudio alias cdrom", None, 0, None)
            ctypes.windll.WINMM.mciSendStringW("set cdrom door open", None, 0, None)
            time.sleep(2)
            ctypes.windll.WINMM.mciSendStringW("set cdrom door closed", None, 0, None)
            time.sleep(3)
            ctypes.windll.WINMM.mciSendStringW("close cdrom", None, 0, None)
            
            if verify_device_access(device):
                return True
                
        except Exception as e:
            logger.error(f"Device reset error: {e}")
            if attempt < max_retries - 1:
                continue
    
    return False

def scan_disc(device: Path, max_retries: int = 3, timeout: int = 300) -> dict:
    """Scan DVD and return title information
    
    Args:
        device: Path to DVD device
        max_retries: Number of scan attempts (default: 3)
        timeout: Scan timeout in seconds (default: 300)
    """
    titles = {}
    scan_output = ""
    
    for attempt in range(max_retries):
        try:
            cmd = [
                'HandBrakeCLI',
                '--verbose', '1',
                '--scan',
                '--title', '0',
                '--no-dvdnav',  # Try without dvdnav first
                '-i', str(device)
            ]
            
            logger.info(f"Scan attempt {attempt + 1}/{max_retries}: {' '.join(cmd)}")
            rprint(f"[yellow]Scanning disc (attempt {attempt + 1})...[/yellow]")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout  # Use passed timeout value
            )
            
            scan_output = result.stdout + result.stderr
            
            # If first attempt fails, try with dvdnav enabled
            if attempt == 1 and not titles:
                cmd[4] = '--dvdnav'  # Enable dvdnav for better read on damaged discs
                logger.info("Retrying scan with dvdnav enabled")
            
            # Parse titles from scan output
            current_title = None
            current_section = None
            
            for line in scan_output.splitlines():
                line = line.strip()
                
                # Look for title headers
                if line.startswith('+ title'):
                    match = re.search(r'\+ title (\d+):', line)
                    if match:
                        current_title = int(match.group(1))
                        current_section = None
                        titles[current_title] = {
                            'duration': 0,
                            'chapters': [],
                            'audio': [],
                            'subtitles': []
                        }
                        continue
                
                if not current_title:
                    continue
                    
                # Track what section we're in
                if line.startswith('+ chapters:'):
                    current_section = 'chapters'
                    continue
                elif line.startswith('+ audio tracks:'):
                    current_section = 'audio'
                    continue
                elif line.startswith('+ subtitle tracks:'):
                    current_section = 'subtitles'
                    continue
                
                # Parse duration (appears right after title)
                if line.startswith('+ duration:'):
                    match = re.search(r'duration: (\d{2}):(\d{2}):(\d{2})', line)
                    if match:
                        hours, minutes, seconds = map(int, match.groups())
                        duration = hours * 3600 + minutes * 60 + seconds
                        titles[current_title]['duration'] = duration
                    continue
                
                # Parse chapters
                if current_section == 'chapters' and line.startswith('+ '):
                    match = re.search(r'\+ (\d+): duration (\d{2}):(\d{2}):(\d{2})', line)
                    if match:
                        chap_num = int(match.group(1))
                        h, m, s = map(int, match.groups()[1:])
                        chap_duration = h * 3600 + m * 60 + s
                        if chap_duration > 0:  # Only add non-zero duration chapters
                            titles[current_title]['chapters'].append(
                                (chap_num, chap_duration)
                            )
            
            # Filter out invalid titles
            filtered_titles = {}
            for num, info in titles.items():
                duration_min = info['duration'] / 60
                valid_chapters = [c for c in info['chapters'] if c[1] > 15]  # >15 seconds
                
                # Must have duration and at least one valid chapter
                if duration_min >= 0.25 and valid_chapters:  # 15 seconds minimum
                    info['chapters'] = valid_chapters  # Update to only valid chapters
                    filtered_titles[num] = info
                    logger.info(f"Title {num}: {duration_min:.1f} minutes, "
                              f"{len(valid_chapters)} valid chapters")
            
            if filtered_titles:
                return filtered_titles
            else:
                logger.warning(f"No valid titles found on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    time.sleep(5)  # Wait before retry
                    continue
                raise DVDError("No valid titles found after all attempts")
                
        except subprocess.TimeoutExpired:
            logger.error(f"Scan timed out on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                time.sleep(5)  # Wait before retry
                continue
            raise DVDError("Scan timed out after all attempts - disc may be damaged")
            
        except Exception as e:
            logger.error(f"Scan failed on attempt {attempt + 1}: {e}")
            logger.error(f"Last scan output:\n{scan_output}")
            if attempt < max_retries - 1:
                rprint("[yellow]Scan failed, retrying...[/yellow]")
                time.sleep(5)  # Wait before retry
                continue
            raise DVDError(f"Could not scan disc after all attempts: {e}")
    
    return {}

def get_tvdb_token() -> str:
    """Get TVDB API token"""
    try:
        response = requests.post(
            f"{CONFIG['TVDB_API_URL']}/login",
            json={
                'apikey': CONFIG['TVDB_API_KEY'],
                'pin': CONFIG['TVDB_API_KEY']  # API key is used as PIN for v4
            }
        )
        response.raise_for_status()
        return response.json()['data']['token']
    except Exception as e:
        raise DVDError(f"Failed to get TVDB token: {e}")

def get_show_info_from_path(path: Path) -> tuple[str, int]:
    """Extract show name and season from path"""
    try:
        # Get season from current folder
        season_match = re.search(r'season[_ ]?(\d+)', path.name.lower())
        season_num = int(season_match.group(1)) if season_match else None
        
        # Get show name from parent folder
        show_name = None
        if path.parent.exists():
            show_dir = path.parent.name
            # Clean up common suffixes and special characters
            show_name = re.sub(r'\s*\(\d{4}\).*$', '', show_dir)  # Remove year and anything after
            show_name = re.sub(r'[._]', ' ', show_name).strip()
        
        return show_name, season_num
    except Exception as e:
        logger.warning(f"Failed to extract info from path: {e}")
        return None, None

def get_tvdb_info(show_name: str, season_num: int) -> dict:
    """Get show information from TVDB API"""
    token = get_tvdb_token()
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    try:
        # First search for the show
        response = requests.get(
            f"{CONFIG['TVDB_API_URL']}/search",
            headers=headers,
            params={'query': show_name, 'type': 'series'}
        )
        response.raise_for_status()
        
        # Get first matching show
        search_data = response.json()
        if not search_data.get('data'):
            raise DVDError(f"Show not found: {show_name}")
        
        # Sanitize series ID - remove 'series-' prefix if present
        raw_id = search_data['data'][0]['id']
        show_id = raw_id.replace('series-', '') if isinstance(raw_id, str) else raw_id
        
        # Get episodes for the specific season
        response = requests.get(
            f"{CONFIG['TVDB_API_URL']}/series/{show_id}/episodes/official",
            headers=headers,
            params={'season': season_num}
        )
        response.raise_for_status()
        
        season_data = response.json()
        if not season_data.get('data', {}).get('episodes'):
            raise DVDError(f"No episodes found for season {season_num}")
            
        episodes = season_data['data']['episodes']
        
        # Extract episode numbers and runtimes
        episode_numbers = []
        runtimes = []
        
        for episode in episodes:
            if episode.get('number'):
                episode_numbers.append(int(episode['number']))
            if episode.get('runtime'):
                try:
                    runtime = int(episode['runtime'])
                    if runtime > 0:
                        runtimes.append(runtime)
                except (ValueError, TypeError):
                    continue
        
        if not runtimes:
            # Default to 30 minutes if no runtimes found
            runtimes = [30]
        
        return {
            'show_name': search_data['data'][0].get('name', show_name),
            'season': season_num,
            'episode_count': max(episode_numbers) if episode_numbers else 0,
            'min_length': min(runtimes),
            'max_length': max(runtimes)
        }
        
    except requests.exceptions.RequestException as e:
        logger.error(f"TVDB API request failed: {e}")
        raise DVDError(f"TVDB API request failed: {e}")
    except Exception as e:
        logger.error(f"TVDB API error details: {e}")
        logger.error(f"Response content: {getattr(response, 'text', 'N/A')}")
        raise DVDError(f"TVDB API error: {e}")

def get_tmdb_info(movie_name: str) -> str:
    """Get movie information from TMDB API"""
    try:
        response = requests.get(
            f"{CONFIG['TMDB_API_URL']}/search/movie",
            params={
                'api_key': CONFIG['TMDB_API_KEY'],
                'query': movie_name
            }
        )
        response.raise_for_status()
        
        movie = response.json()['results'][0]
        return f"{movie['title']} ({movie['release_date'][:4]})"
        
    except Exception as e:
        raise DVDError(f"TMDB API error: {e}")

def verify_handbrake_can_read(device: Path) -> bool:
    """Verify HandBrake can read the DVD"""
    try:
        # Use quick scan with single retry
        titles = scan_disc(device, max_retries=1, timeout=30)
        return bool(titles)
    except Exception as e:
        logger.error(f"HandBrake read test failed: {e}")
        return False

def rip_title(device: Path, title_num: int, output_file: Path, 
              chapter: int = None, progress_callback=None) -> bool:
    """Rip a title (or chapter) from DVD"""
    # Create output directory if it doesn't exist
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Create log directory
    log_dir = Path.home() / '.dvd_ripper' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"handbrake_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    # Base command with DVD-specific settings
    base_cmd = [
        'HandBrakeCLI',
        '--verbose', '1',
        '-i', str(device),
        '-o', str(output_file),
        '--title', str(title_num),
        
        # DVD-specific settings
        '--no-dvdnav',  # Enable dvdnav for better DVD reading
        
        # Container settings
        '--format', 'mkv',
        '--markers',  # Keep chapter markers
        
        # Video settings
        '--vfr',  # Variable framerate
        '--decomb',  # Better than deinterlace
        '--quality', '0',
        
        # Audio settings - DVD specific
        '--all-audio',
        '--aencoder', 'copy:ac3',  # Only copy AC3 which is DVD standard
        '--audio-fallback', 'ac3',
        '--audio-copy-mask', 'ac3',
        
        # Subtitle settings - DVD specific
        '--subtitle-lang-list', 'eng',
        '--first-subtitle', 'scan',
    ]
    
    # Add chapter range if specified
    if chapter:
        base_cmd.extend(['--chapters', f"{chapter}:{chapter}"])
    
    # Try NVENC first, fallback to x264
    try:
        subprocess.run(['nvidia-smi'], capture_output=True, check=True)
        logger.info("NVIDIA GPU detected, trying NVENC")
        cmd = base_cmd + [
            '--encoder', 'nvenc_h265_10bit',  # Use H.264 for better compatibility
            '--encoder-preset', 'lossless',
            '--enable-hw-decoding', 'nvdec'
        ]
    except subprocess.SubprocessError:
        logger.info("No NVIDIA GPU found, using CPU encoding")
        cmd = base_cmd + [
            '--encoder', 'x264',
            '--encoder-preset', 'slow'
        ]

    logger.info(f"Starting DVD rip of title {title_num}")
    logger.info(f"Command: {' '.join(cmd)}")
    logger.info(f"Logging to: {log_file}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Clear system cache between attempts to prevent memory issues
            if attempt > 0:
                try:
                    subprocess.run(['sync'])
                    with open('/proc/sys/vm/drop_caches', 'w') as f:
                        f.write('3')
                except Exception as e:
                    logger.warning(f"Failed to clear caches: {e}")
                time.sleep(10)  # Give system time to stabilize
            
            with open(log_file, 'a') as log:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1
                )
                
                encoding_started = False
                last_progress = 0
                last_progress_time = time.time()
                progress_stall_count = 0
                
                while True:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                        
                    log.write(line)
                    log.flush()
                    
                    # Look for DVD-specific errors
                    if any(err in line.lower() for err in [
                        "dvd read error",
                        "error reading nav packet",
                        "invalid ifo",
                        "error opening vob"
                    ]):
                        # Try remounting disc before giving up
                        if attempt < max_retries - 1:
                            process.kill()
                            remount_device(device)
                            raise DVDError(f"DVD read error (will retry): {line.strip()}")
                        else:
                            process.kill()
                            raise DVDError(f"DVD read error: {line.strip()}")
                    
                    # Look for progress indicators
                    if "Encoding" in line and "%" in line:
                        if not encoding_started:
                            encoding_started = True
                            rprint("[green]Encoding started[/green]")
                        try:
                            progress = float(line.split('%')[0].split()[-1])
                            
                            # Check for progress stalls
                            if progress == last_progress:
                                progress_stall_count += 1
                                if progress_stall_count > 60:  # 1 minute with no progress
                                    if attempt < max_retries - 1:
                                        process.kill()
                                        raise DVDError("Progress stalled (will retry)")
                            else:
                                progress_stall_count = 0
                                last_progress = progress
                                last_progress_time = time.time()
                            
                            if progress_callback:
                                progress_callback(progress)
                        except ValueError:
                            pass
                
                # Wait for process with timeout
                try:
                    process.wait(timeout=600)  # 10 minute timeout per chapter
                except subprocess.TimeoutExpired:
                    process.kill()
                    if attempt < max_retries - 1:
                        raise DVDError("Rip timed out (will retry)")
                    else:
                        raise DVDError("Rip timed out")
                
                # Only check file after process has completed
                if process.returncode == 0:
                    time.sleep(2)  # Give filesystem time to finish
                    if output_file.exists():
                        file_size = output_file.stat().st_size
                        if file_size > 1000000:  # 1MB minimum
                            logger.info(f"Successfully ripped to {output_file} ({file_size/1024/1024:.1f}MB)")
                            return True
                    raise DVDError("Output file missing or too small")
                else:
                    raise DVDError(f"HandBrake failed with code {process.returncode}")
                    
        except DVDError as e:
            logger.error(f"Rip error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                rprint(f"[yellow]Rip failed ({e}), retrying...[/yellow]")
                if output_file.exists():
                    output_file.unlink()
                time.sleep(5)
                continue
            raise

    return False

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

def process_tv_show(output_dir: Path) -> bool:
    """Process TV show disc
    Returns True if any files were created"""
    # Get show info from path or prompt user
    show_name, season_num = get_show_info_from_path(output_dir)
    
    if not show_name:
        show_name = Prompt.ask("Enter show name")
    if not season_num:
        season_num = int(Prompt.ask("Enter season number"))
    
    rprint(f"\nShow: {show_name}")
    rprint(f"Season: {season_num}")
    
    # Get show info from TVDB
    show_info = get_tvdb_info(show_name, season_num)
    rprint(f"Episodes: {show_info['episode_count']}")
    rprint(f"Episode Length: {show_info['min_length']}-{show_info['max_length']} minutes")
    
    # Ask for rip mode
    rip_mode = Prompt.ask(
        "Rip by episode or compilation?",
        choices=["episode", "compilation"],
        default="episode"
    )
    
    start_episode = int(Prompt.ask("Enter starting episode number"))
    
    device = find_dvd_device()
    
    # Get episode ranges from user
    episode_ranges = get_episode_ranges()
    
    while True:
        try:
            # Give the drive time to spin up
            time.sleep(5)
            
            # Basic device check
            if not verify_device_access(device):
                raise DVDError("DVD device not ready")
            
            titles = scan_disc(device)
            
            if rip_mode == "episode":
                # Filter for episode-length titles
                episode_titles = filter_episodes(
                    titles, 
                    show_info['min_length'],
                    show_info['max_length'],
                    device,
                    episode_ranges
                )
                
                if not episode_titles:
                    raise DVDError("No valid episode titles found")
                    
                logger.info(f"Found {len(episode_titles)} matching episode titles")
                
                # Initialize season tracking and progress counter
                current_season = season_num
                files_created = False
                total_rips = len(episode_titles)
                current_rip = 1
                
                for title_num in sorted(episode_titles.keys()):
                    # Get next available episode slot
                    season_num, current_episode, output_file = get_next_season_episode(
                        output_dir, 
                        season_num,
                        show_info['episode_count']
                    )
                    
                    # Update show info if season changed
                    if season_num != current_season:
                        show_info = get_tvdb_info(show_name, season_num)
                        current_season = season_num
                        logger.info(f"Moving to season {season_num}")
                    
                    rprint(f"\n[cyan]Ripping {current_rip}/{total_rips}:[/cyan] "
                          f"Title {title_num} as episode {current_episode}")
                    
                    with Progress() as progress:
                        task = progress.add_task(
                            f"Ripping S{season_num:02d}E{current_episode:02d}",
                            total=100
                        )
                        
                        if rip_title(
                            device,
                            title_num,
                            output_file,
                            progress_callback=lambda p: progress.update(task, completed=p)
                        ):
                            files_created = True
                        else:
                            rprint(f"[red]Failed to rip title {title_num}[/red]")
                    
                    current_rip += 1
                
            else:  # compilation mode
                # Filter for compilation titles
                compilation_titles = filter_compilations(
                    titles,
                    device,
                    episode_ranges
                )
                
                if not compilation_titles:
                    raise DVDError("No valid compilation titles found")
                    
                logger.info(f"Found {len(compilation_titles)} compilation titles")
                
                # Count total chapters to rip
                total_rips = sum(len(info['chapters']) for info in compilation_titles.values())
                current_rip = 1
                files_created = False
                
                for title_num, info in sorted(compilation_titles.items()):
                    for chapter_num, duration in info['chapters']:
                        # Get next available episode slot
                        season_num, current_episode, output_file = get_next_season_episode(
                            output_dir,
                            season_num,
                            show_info['episode_count']
                        )
                        
                        # Update show info if season changed
                        if season_num != current_season:
                            show_info = get_tvdb_info(show_name, season_num)
                            current_season = season_num
                            logger.info(f"Moving to season {season_num}")
                        
                        rprint(f"\n[cyan]Ripping {current_rip}/{total_rips}:[/cyan] "
                              f"Title {title_num} Chapter {chapter_num} as episode {current_episode}")
                        
                        with Progress() as progress:
                            task = progress.add_task(
                                f"Ripping S{season_num:02d}E{current_episode:02d}",
                                total=100
                            )
                            
                            if rip_title(
                                device,
                                title_num,
                                output_file,
                                chapter=chapter_num,
                                progress_callback=lambda p: progress.update(task, completed=p)
                            ):
                                files_created = True
                            else:
                                rprint(f"[red]Failed to rip title {title_num} chapter {chapter_num}[/red]")
                        
                        current_rip += 1
            
            if not files_created:
                raise DVDError("No files were successfully created")
            
            return True  # Return True instead of showing completion message
            
        except DVDError as e:
            rprint(f"[red]Error: {e}[/red]")
            if not Confirm.ask("Try again?"):
                break
    
    return False

def process_movie(output_dir: Path):
    """Process movie disc"""
    movie_name = Prompt.ask("Enter movie name")
    movie_info = get_tmdb_info(movie_name)
    
    device = find_dvd_device()
    titles = scan_disc(device)
    
    # Find title with most chapters
    main_title = max(titles.items(), key=lambda x: len(x[1]['chapters']))[0]
    
    output_file = output_dir / f"{movie_info}.mkv"
    
    with Progress() as progress:
        task = progress.add_task(f"Ripping {movie_info}", total=100)
        rip_title(device, main_title, output_file,
                 progress_callback=lambda p: progress.update(task, completed=p))

def main():
    """Main entry point"""
    try:
        # Initialize
        cleanup_previous_session()
        check_dependencies()
        
        # Get output location
        output_dir = Path(Prompt.ask("Enter output location"))
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Process discs until user is done
        while True:
            try:
                device = find_dvd_device()
                
                if not ensure_device_ready(device):
                    raise DVDError("Failed to prepare device")
                
                if process_tv_show(output_dir):
                    # Prompt for next disc only if current one succeeded
                    if not Confirm.ask("\nInsert next disc and continue?"):
                        break
                else:
                    # If no files created, ask to retry
                    if not Confirm.ask("Try again?"):
                        break
                        
            except DVDError as e:
                rprint(f"[red]Error: {e}[/red]")
                if not Confirm.ask("Try again?"):
                    break
                    
        # Show completion message only once at the end
        rprint("\n[green]Ripping complete![/green]")
        rprint("[blue]You're awesome![/blue]")
        
    except Exception as e:
        rprint(f"[red]Unexpected error: {e}[/red]")
        logger.error(f"Unexpected error", exc_info=True)
        sys.exit(1)

def cleanup_previous_session():
    """Clean up any leftover files and processes on Windows"""
    try:
        # Check for and remove lock file
        lock_file = Path(tempfile.gettempdir()) / "dvd_ripper.lock"
        if lock_file.exists():
            try:
                with open(lock_file) as f:
                    pid = int(f.read().strip())
                try:
                    import psutil
                    if psutil.pid_exists(pid):
                        process = psutil.Process(pid)
                        if "HandBrakeCLI" in process.name():
                            rprint(f"[yellow]Found running DVD ripper process (PID: {pid})[/yellow]")
                            if Confirm.ask("Kill existing process?"):
                                process.terminate()
                                process.wait(timeout=5)
                except psutil.NoSuchProcess:
                    lock_file.unlink()
            except (ValueError, IOError):
                lock_file.unlink()

        # Clean up temporary files
        temp_dir = Path(tempfile.gettempdir()) / "dvd_ripper"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            
    except Exception as e:
        logger.warning(f"Cleanup warning: {e}")

def verify_ripped_file(file_path: Path) -> bool:
    """Verify ripped file is valid"""
    try:
        cmd = ['ffmpeg', '-v', 'error', '-i', str(file_path), '-f', 'null', '-']
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"Verification failed: {e}")
        return False

def remount_device(device: Path):
    """Attempt to remount DVD device on Windows"""
    import ctypes
    try:
        # Eject/close drive using Windows API
        ctypes.windll.WINMM.mciSendStringW(f"open {device} type cdaudio alias cdrom", None, 0, None)
        ctypes.windll.WINMM.mciSendStringW("set cdrom door open", None, 0, None)
        time.sleep(2)
        ctypes.windll.WINMM.mciSendStringW("set cdrom door closed", None, 0, None)
        time.sleep(3)
        ctypes.windll.WINMM.mciSendStringW("close cdrom", None, 0, None)
    except Exception as e:
        logger.warning(f"Remount failed: {e}")

def handle_stalled_rip(process, start_time: float, stall_timeout: int = 30) -> bool:
    """Check for and handle stalled rips"""
    if time.time() - start_time > stall_timeout:
        rprint("[yellow]Rip appears stalled, attempting recovery...[/yellow]")
        try:
            process.kill()
            return True
        except:
            return False
    return False

def sample_title(device: Path, title_num: int, duration: float, chapter: int = None) -> tuple[Path, int]:
    """Create a 5-second sample from middle of title/chapter"""
    temp_dir = Path("/tmp/dvd_ripper")
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # Calculate middle point
    mid_point = duration / 2
    start_time = max(0, mid_point - 2.5)
    
    # Create sample file
    sample_file = temp_dir / f"title_{title_num}_{'ch'+str(chapter) if chapter else ''}_sample.mkv"
    
    cmd = [
        'HandBrakeCLI',
        '--verbose', '0',
        '-i', str(device),
        '-o', str(sample_file),
        '--title', str(title_num),
        '--start-at', f'duration:{start_time}',
        '--stop-at', 'duration:5',
        '--no-dvdnav',
        '--format', 'mkv',
        '--quality', '0'
    ]
    
    # Add chapter if specified
    if chapter is not None:
        cmd.extend(['--chapters', f"{chapter}:{chapter}"])
    
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=30)
        return sample_file, sample_file.stat().st_size
    except Exception as e:
        logger.error(f"Failed to create sample for title {title_num}: {e}")
        if sample_file.exists():
            sample_file.unlink()
        return None, 0

def filter_episodes(titles: dict, min_length: int, max_length: int, 
                   device: Path, episode_ranges: dict) -> dict:
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
            # Group by exact duration (no rounding)
            if duration not in duration_groups:
                duration_groups[duration] = []
            duration_groups[duration].append((num, info))
            logger.info(f"Title {num}: {duration_min:.2f} minutes, {len(info['chapters'])} chapters")
    
    # Second pass - analyze duration groups
    for duration, group in sorted(duration_groups.items()):
        duration_min = duration / 60
        logger.info(f"\nFound {len(group)} titles with duration {duration_min:.2f} minutes:")
        
        if len(group) >= 2:
            # Create samples for comparison
            samples = {}
            unique_titles = set()
            
            rprint(f"\n[yellow]Analyzing {len(group)} titles of {duration_min:.2f} minutes...[/yellow]")
            
            # Sort by chapter count (prefer more chapters for better chapter markers)
            sorted_group = sorted(group, key=lambda x: len(x[1]['chapters']), reverse=True)
            
            for num, info in sorted_group:
                sample_file, sample_size = sample_title(device, num, duration)
                if sample_size > 0:
                    if sample_size not in samples:
                        # This is a unique episode
                        samples[sample_size] = num
                        unique_titles.add(num)
                        # Show which range this episode falls into
                        for range_name, (min_sec, max_sec) in episode_ranges.items():
                            if min_sec <= duration <= max_sec:
                                rprint(f"[green]Title {num} appears to be unique "
                                      f"({range_name}, {duration_min:.2f} min, "
                                      f"{len(info['chapters'])} chapters)[/green]")
                                break
                    else:
                        # This is a duplicate
                        original = samples[sample_size]
                        rprint(f"[yellow]Title {num} appears to be duplicate of title {original}[/yellow]")
                
                # Clean up sample file
                if sample_file and sample_file.exists():
                    sample_file.unlink()
            
            # Add unique titles to episode list
            for num in unique_titles:
                for title_num, info in group:
                    if title_num == num:
                        episode_titles[num] = info
                        break
        else:
            # Single title in group - add it directly
            num, info = group[0]
            episode_titles[num] = info
            # Show which range this episode falls into
            for range_name, (min_sec, max_sec) in episode_ranges.items():
                if min_sec <= duration <= max_sec:
                    rprint(f"[green]Title {num} added ({range_name}, {duration_min:.2f} min, "
                          f"{len(info['chapters'])} chapters)[/green]")
                    break
    
    if not episode_titles:
        logger.warning("No matching episode groups found - check duration criteria")
    else:
        logger.info(f"\nSelected {len(episode_titles)} unique episode titles:")
        for num, info in sorted(episode_titles.items()):
            duration_min = info['duration'] / 60
            # Show which range each selected episode falls into
            for range_name, (min_sec, max_sec) in episode_ranges.items():
                if min_sec <= info['duration'] <= max_sec:
                    logger.info(f"Title {num}: {duration_min:.2f} minutes ({range_name}), "
                              f"{len(info['chapters'])} chapters")
                    break
    
    return episode_titles

def filter_compilations(titles: dict, device: Path, episode_ranges: dict) -> dict:
    """Filter titles and chapters for compilations"""
    logger.info("Filtering for compilation titles")
    
    compilation_titles = {}
    
    for num, info in titles.items():
        duration = info['duration']
        duration_min = duration / 60
        valid_chapters = []
        
        # Only look at longer titles (> 90 minutes)
        if duration_min > 90:
            # Check each chapter's duration against our episode ranges
            for chapter_num, chapter_duration in info['chapters']:
                chapter_min = chapter_duration / 60
                
                # Check if chapter duration matches any episode range
                for range_name, (min_sec, max_sec) in episode_ranges.items():
                    if min_sec <= chapter_duration <= max_sec:
                        valid_chapters.append((chapter_num, chapter_duration))
                        logger.info(f"Title {num} Chapter {chapter_num}: "
                                  f"{chapter_min:.2f} minutes ({range_name})")
                        break
            
            # Only include titles that have valid episode-length chapters
            if valid_chapters:
                info['chapters'] = valid_chapters
                compilation_titles[num] = info
                logger.info(f"Found compilation title {num}: "
                          f"{duration_min:.2f} minutes, "
                          f"{len(valid_chapters)} valid episodes")
    
    if compilation_titles:
        # Present choices to user
        rprint("\n[yellow]Found potential compilation titles:[/yellow]")
        for num, info in compilation_titles.items():
            rprint(f"\nTitle {num} ({info['duration']/60:.1f} minutes):")
            for chapter_num, duration in info['chapters']:
                # Show which range each chapter falls into
                for range_name, (min_sec, max_sec) in episode_ranges.items():
                    if min_sec <= duration <= max_sec:
                        rprint(f"  Chapter {chapter_num}: {duration/60:.2f} minutes ({range_name})")
                        break
        
        # Get user input for titles
        while True:
            choice = Prompt.ask(
                "Which titles would you like to rip? (comma-separated numbers, ranges allowed e.g. '1,3-5,7')"
            )
            
            try:
                chosen_titles = {}
                # Split by comma and process each part
                for part in choice.replace(' ', '').split(','):
                    if '-' in part:
                        start, end = map(int, part.split('-'))
                        for num in range(start, end + 1):
                            if num in compilation_titles:
                                # Create samples for chapters in this title
                                title_info = compilation_titles[num]
                                unique_chapters = []
                                samples = {}
                                
                                rprint(f"\n[yellow]Analyzing chapters in title {num}...[/yellow]")
                                for chapter_num, duration in title_info['chapters']:
                                    sample_file, sample_size = sample_title(
                                        device, num, duration, 
                                        chapter=chapter_num
                                    )
                                    if sample_size > 0:
                                        if sample_size not in samples:
                                            samples[sample_size] = (num, chapter_num)
                                            unique_chapters.append((chapter_num, duration))
                                            rprint(f"[green]Chapter {chapter_num} appears to be unique "
                                                  f"({duration/60:.2f} min)[/green]")
                                        else:
                                            orig_num, orig_chap = samples[sample_size]
                                            rprint(f"[yellow]Chapter {chapter_num} appears to be "
                                                  f"duplicate of title {orig_num} chapter {orig_chap}[/yellow]")
                                    
                                    if sample_file and sample_file.exists():
                                        sample_file.unlink()
                                
                                if unique_chapters:
                                    title_info = title_info.copy()
                                    title_info['chapters'] = unique_chapters
                                    chosen_titles[num] = title_info
                            else:
                                rprint(f"[yellow]Warning: Title {num} not found, skipping[/yellow]")
                    else:
                        # Handle single number (same logic as above)
                        num = int(part)
                        if num in compilation_titles:
                            # Create samples for chapters in this title
                            title_info = compilation_titles[num]
                            unique_chapters = []
                            samples = {}
                            
                            rprint(f"\n[yellow]Analyzing chapters in title {num}...[/yellow]")
                            for chapter_num, duration in title_info['chapters']:
                                sample_file, sample_size = sample_title(
                                    device, num, duration, 
                                    chapter=chapter_num
                                )
                                if sample_size > 0:
                                    if sample_size not in samples:
                                        samples[sample_size] = (num, chapter_num)
                                        unique_chapters.append((chapter_num, duration))
                                        rprint(f"[green]Chapter {chapter_num} appears to be unique "
                                              f"({duration/60:.2f} min)[/green]")
                                    else:
                                        orig_num, orig_chap = samples[sample_size]
                                        rprint(f"[yellow]Chapter {chapter_num} appears to be "
                                              f"duplicate of title {orig_num} chapter {orig_chap}[/yellow]")
                                    
                                    if sample_file and sample_file.exists():
                                        sample_file.unlink()
                                
                            if unique_chapters:
                                title_info = title_info.copy()
                                title_info['chapters'] = unique_chapters
                                chosen_titles[num] = title_info
                        else:
                            rprint(f"[yellow]Warning: Title {num} not found, skipping[/yellow]")
                
                if chosen_titles:
                    rprint("\n[green]Selected titles and their unique chapters:[/green]")
                    for num, info in chosen_titles.items():
                        rprint(f"\nTitle {num}:")
                        for chapter_num, duration in info['chapters']:
                            for range_name, (min_sec, max_sec) in episode_ranges.items():
                                if min_sec <= duration <= max_sec:
                                    rprint(f"  Chapter {chapter_num}: {duration/60:.2f} minutes ({range_name})")
                                    break
                    return chosen_titles
                else:
                    rprint("[red]No valid titles selected. Please try again.[/red]")
                    
            except ValueError:
                rprint("[red]Invalid input format. Please use numbers and ranges (e.g., '1,3-5,7')[/red]")
    
    return {}

if __name__ == "__main__":
    main() 