#!/usr/bin/env python3
"""
tfsm_batch_processor.py - Batch process capture files using TextFSM auto-matching

Processes all ._output files in capture folders, finds best matching templates,
and outputs parsed JSON results mirroring the original folder structure.
"""

import os
import sys
import json
import time
import sqlite3
import textfsm
import io
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import click
import threading
from contextlib import contextmanager


# === ThreadSafeConnection and TextFSMAutoEngine from tfsm_fire.py ===

class ThreadSafeConnection:
    """Thread-local storage for SQLite connections"""

    def __init__(self, db_path: str, verbose: bool = False):
        self.db_path = db_path
        self.verbose = verbose
        self._local = threading.local()

    @contextmanager
    def get_connection(self):
        """Get a thread-local connection"""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(self.db_path)
            self._local.connection.row_factory = sqlite3.Row
            if self.verbose:
                click.echo(f"Created new connection in thread {threading.get_ident()}")
        try:
            yield self._local.connection
        except Exception as e:
            if hasattr(self._local, 'connection'):
                self._local.connection.close()
                delattr(self._local, 'connection')
            raise e

    def close_all(self):
        """Close connection if it exists for current thread"""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            delattr(self._local, 'connection')


class TextFSMAutoEngine:
    def __init__(self, db_path: str, verbose: bool = False):
        self.db_path = db_path
        self.verbose = verbose
        self.connection_manager = ThreadSafeConnection(db_path, verbose)

    def _calculate_template_score(
            self,
            parsed_data: List[Dict],
            template: sqlite3.Row,
            raw_output: str
    ) -> float:
        """Score template match quality (0-100 scale)."""
        if not parsed_data:
            return 0.0

        num_records = len(parsed_data)
        num_fields = len(parsed_data[0].keys()) if parsed_data else 0
        is_version_cmd = 'version' in template['cli_command'].lower()

        # Factor 1: Record Count (0-30 points)
        if is_version_cmd:
            record_score = 30.0 if num_records == 1 else max(0, 15 - (num_records - 1) * 5)
        else:
            if num_records >= 10:
                record_score = 30.0
            elif num_records >= 3:
                record_score = 20.0 + (num_records - 3) * (10.0 / 7.0)
            else:
                record_score = num_records * 10.0

        # Factor 2: Field Richness (0-30 points)
        if num_fields >= 10:
            field_score = 30.0
        elif num_fields >= 6:
            field_score = 20.0 + (num_fields - 6) * 2.5
        elif num_fields >= 3:
            field_score = 10.0 + (num_fields - 3) * (10.0 / 3.0)
        else:
            field_score = num_fields * 5.0

        # Factor 3: Population Rate (0-25 points)
        total_cells = num_records * num_fields
        populated_cells = 0
        for record in parsed_data:
            for value in record.values():
                if value is not None and str(value).strip():
                    populated_cells += 1
        population_rate = populated_cells / total_cells if total_cells > 0 else 0
        population_score = population_rate * 25.0

        # Factor 4: Consistency (0-15 points)
        if num_records > 1:
            field_fill_counts = {key: 0 for key in parsed_data[0].keys()}
            for record in parsed_data:
                for key, value in record.items():
                    if value is not None and str(value).strip():
                        field_fill_counts[key] += 1
            consistent_fields = sum(
                1 for count in field_fill_counts.values()
                if count == 0 or count == num_records
            )
            consistency_rate = consistent_fields / num_fields if num_fields > 0 else 0
            consistency_score = consistency_rate * 15.0
        else:
            consistency_score = 15.0

        return record_score + field_score + population_score + consistency_score

    def find_best_template(self, device_output: str, filter_string: Optional[str] = None) -> Tuple[
        Optional[str], Optional[List[Dict]], float]:
        """Try filtered templates against the output and return the best match."""
        best_template = None
        best_parsed_output = None
        best_score = 0

        with self.connection_manager.get_connection() as conn:
            templates = self.get_filtered_templates(conn, filter_string)

            for template in templates:
                try:
                    textfsm_template = textfsm.TextFSM(io.StringIO(template['textfsm_content']))
                    parsed = textfsm_template.ParseText(device_output)
                    parsed_dicts = [dict(zip(textfsm_template.header, row)) for row in parsed]
                    score = self._calculate_template_score(parsed_dicts, template, device_output)

                    if score > best_score:
                        best_score = score
                        best_template = template['cli_command']
                        best_parsed_output = parsed_dicts

                except Exception:
                    continue

        return best_template, best_parsed_output, best_score

    def get_filtered_templates(self, connection: sqlite3.Connection, filter_string: Optional[str] = None):
        """Get filtered templates from database using provided connection."""
        cursor = connection.cursor()
        if filter_string:
            filter_terms = filter_string.replace('-', '_').split('_')
            query = "SELECT * FROM templates WHERE 1=1"
            params = []
            for term in filter_terms:
                if term and len(term) > 2:
                    query += " AND cli_command LIKE ?"
                    params.append(f"%{term}%")
            cursor.execute(query, params)
        else:
            cursor.execute("SELECT * FROM templates")
        return cursor.fetchall()

    def __del__(self):
        self.connection_manager.close_all()


# === Batch Processor ===

@dataclass
class ProcessingStats:
    """Track processing statistics"""
    total_files: int = 0
    processed: int = 0
    matched: int = 0
    below_threshold: int = 0
    failed: int = 0
    skipped_empty: int = 0
    total_records: int = 0
    processing_time: float = 0.0

    # Per-folder stats
    folder_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Template usage
    template_hits: Dict[str, int] = field(default_factory=dict)

    # Score distribution
    scores: List[float] = field(default_factory=list)


# Map folder names to filter strings for better template matching
# Can be a single string or a list of strings to try multiple filters
FOLDER_FILTER_MAP = {
    'arp': 'arp',
    'authentication': 'authentication',
    'authorization': 'authorization',
    'bgp-neighbor': 'bgp_neighbor',
    'bgp-summary': 'bgp_summary',
    'bgp-table': 'bgp_table',
    'bgp-table-detail': 'bgp_table',
    'cdp': 'cdp',
    'cdp-detail': 'cdp_neighbor_detail',
    'cdp-detail-ios': 'cdp_neighbor_detail',
    'cdp-detail-nexus': 'cdp_neighbor_detail',
    'cdp_neighbors': 'cdp_neighbor',
    'configs': None,  # No template for configs
    'console': 'line',
    'eigrp-neighbor': 'eigrp_neighbor',
    'interfaces': 'interface',
    'int-status': ['interface_status', 'interface_brief'],
    'inventory': 'inventory',
    'ip_ssh': 'ssh',
    'license': 'license',
    'license_save': 'license',
    'lldp': ['lldp_neighbor', 'lldp_remote'],
    'lldp-detail': ['lldp_neighbor_detail', 'lldp_remote'],
    'lldp_neighbors': ['lldp_neighbor', 'lldp_remote'],
    'mac': ['mac_address', 'mac_table'],
    'mac-aruba': ['mac_address', 'mac_table'],
    'nat': 'nat',
    'ntp_status': ['ntp_status', 'ntp_association'],
    'ospf-neighbor': 'ospf_neighbor',
    'routes': ['route', 'ip_route'],
    'route-table': ['route', 'ip_route'],
    'snmp_server': 'snmp',
    'spanning-tree': 'spanning_tree',
    'syslog': 'logging',
    'tacacs': 'tacacs',
    'version': ['version', 'system_info', 'system'],  # Multi-vendor: Cisco, Palo, ProCurve
    'vlans': 'vlan',
    'vrf': 'vrf',
}


def get_filters_for_folder(folder_name: str) -> List[Optional[str]]:
    """Get the filter strings for a given folder name. Returns a list."""
    result = None

    # Direct match
    if folder_name in FOLDER_FILTER_MAP:
        result = FOLDER_FILTER_MAP[folder_name]
    else:
        # Try lowercase
        lower_name = folder_name.lower()
        if lower_name in FOLDER_FILTER_MAP:
            result = FOLDER_FILTER_MAP[lower_name]
        else:
            # Try to extract meaningful filter from folder name
            clean_name = lower_name.replace('-', '_').replace('.', '_')

            # Check for partial matches
            for key, value in FOLDER_FILTER_MAP.items():
                if key in clean_name or clean_name in key:
                    result = value
                    break

            # Fall back to folder name itself as filter
            if result is None:
                result = clean_name if len(clean_name) > 2 else None

    # Normalize to list
    if result is None:
        return [None]
    elif isinstance(result, list):
        return result
    else:
        return [result]


def find_output_files(capture_dir: Path) -> List[Tuple[Path, str]]:
    """Find all ._output files and their parent folder names."""
    output_files = []

    for root, dirs, files in os.walk(capture_dir):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        for file in files:
            if file.endswith('._output'):
                file_path = Path(root) / file
                # Get the immediate parent folder name (the capture type)
                rel_path = file_path.relative_to(capture_dir)
                folder_name = rel_path.parts[0] if rel_path.parts else 'unknown'
                output_files.append((file_path, folder_name))

    return output_files


def process_file(
        engine: TextFSMAutoEngine,
        file_path: Path,
        folder_name: str,
        min_score: float = 10.0,
        verbose: bool = False
) -> Tuple[Optional[Dict], float, Optional[str]]:
    """Process a single file and return parsed data, score, and template name."""

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        if verbose:
            click.echo(f"  Error reading {file_path}: {e}")
        return None, 0.0, None

    if not content.strip():
        return None, 0.0, None

    # Get all filters for this folder
    filters = get_filters_for_folder(folder_name)

    best_parsed_data = None
    best_score = 0.0
    best_template = None

    # Try each filter and keep the best result
    for filter_string in filters:
        if verbose:
            click.echo(f"  Trying filter: {filter_string}")

        template_name, parsed_data, score = engine.find_best_template(content, filter_string)

        if score > best_score:
            best_score = score
            best_parsed_data = parsed_data
            best_template = template_name
            if verbose:
                click.echo(f"    -> New best: {template_name} (score={score:.1f})")

    return best_parsed_data, best_score, best_template


@click.command()
@click.option('--capture-dir', '-c', required=True, type=click.Path(exists=True),
              help='Path to capture directory')
@click.option('--output-dir', '-o', required=True, type=click.Path(),
              help='Path to output directory for parsed results')
@click.option('--db-path', '-d', required=True, type=click.Path(exists=True),
              help='Path to TextFSM templates database')
@click.option('--min-score', '-m', default=10.0, type=float,
              help='Minimum score threshold (default: 10)')
@click.option('--folder', '-f', default=None, type=str,
              help='Process only this specific folder (e.g., "version")')
@click.option('--verbose', '-v', is_flag=True, help='Verbose output')
@click.option('--dry-run', is_flag=True, help='Show what would be processed without writing files')
def main(capture_dir: str, output_dir: str, db_path: str, min_score: float, folder: str, verbose: bool, dry_run: bool):
    """
    Batch process capture files using TextFSM auto-matching.

    Processes all ._output files in capture folders, finds best matching templates,
    and outputs parsed JSON results mirroring the original folder structure.
    """
    capture_path = Path(capture_dir)
    output_path = Path(output_dir)

    click.echo(click.style("=" * 70, fg='cyan'))
    click.echo(click.style("TextFSM Batch Processor", fg='cyan', bold=True))
    click.echo(click.style("=" * 70, fg='cyan'))
    click.echo(f"Capture Dir:  {capture_path}")
    click.echo(f"Output Dir:   {output_path}")
    click.echo(f"Database:     {db_path}")
    click.echo(f"Min Score:    {min_score}")
    if folder:
        click.echo(f"Folder:       {folder}")
    click.echo(f"Dry Run:      {dry_run}")
    click.echo()

    # Initialize engine
    click.echo("Initializing TextFSM engine...")
    engine = TextFSMAutoEngine(db_path, verbose=False)

    # Find all output files
    click.echo("Scanning for ._output files...")
    output_files = find_output_files(capture_path)

    # Filter to specific folder if requested
    if folder:
        output_files = [(fp, fn) for fp, fn in output_files if fn == folder]
        if not output_files:
            click.echo(click.style(f"No files found in folder '{folder}'", fg='red'))
            return

    stats = ProcessingStats()
    stats.total_files = len(output_files)

    click.echo(f"Found {stats.total_files} files to process")
    click.echo()

    if not dry_run:
        output_path.mkdir(parents=True, exist_ok=True)

    start_time = time.time()

    # Group files by folder for organized processing
    files_by_folder: Dict[str, List[Path]] = {}
    for file_path, folder_name in output_files:
        if folder_name not in files_by_folder:
            files_by_folder[folder_name] = []
        files_by_folder[folder_name].append(file_path)

    # Process each folder
    for folder_name in sorted(files_by_folder.keys()):
        folder_files = files_by_folder[folder_name]
        folder_filters = get_filters_for_folder(folder_name)
        filter_display = ', '.join(f or 'none' for f in folder_filters)

        click.echo(click.style(f"\n[{folder_name}]", fg='yellow', bold=True) +
                   f" ({len(folder_files)} files, filters: {filter_display})")

        folder_stats = {
            'total': len(folder_files),
            'matched': 0,
            'below_threshold': 0,
            'failed': 0,
            'skipped_empty': 0,
            'records': 0,
            'avg_score': 0.0,
            'scores': []
        }

        # Create output subdirectory
        folder_output_path = output_path / folder_name
        if not dry_run:
            folder_output_path.mkdir(parents=True, exist_ok=True)

        for file_path in folder_files:
            stats.processed += 1
            rel_path = file_path.relative_to(capture_path)

            if verbose:
                click.echo(f"  Processing: {rel_path}")

            parsed_data, score, template_name = process_file(
                engine, file_path, folder_name, min_score, verbose
            )

            # Determine outcome
            if parsed_data is None and score == 0.0:
                # Check if file was empty or failed to read
                try:
                    with open(file_path, 'r') as f:
                        if not f.read().strip():
                            stats.skipped_empty += 1
                            folder_stats['skipped_empty'] += 1
                            if verbose:
                                click.echo(f"    -> Empty file, skipped")
                            continue
                except:
                    pass
                stats.failed += 1
                folder_stats['failed'] += 1
                if verbose:
                    click.echo(f"    -> No match found")
                continue

            if score < min_score:
                stats.below_threshold += 1
                folder_stats['below_threshold'] += 1
                if verbose:
                    click.echo(f"    -> Score {score:.1f} below threshold ({min_score})")
                continue

            # Success!
            stats.matched += 1
            folder_stats['matched'] += 1
            stats.scores.append(score)
            folder_stats['scores'].append(score)

            num_records = len(parsed_data) if parsed_data else 0
            stats.total_records += num_records
            folder_stats['records'] += num_records

            # Track template usage
            if template_name:
                stats.template_hits[template_name] = stats.template_hits.get(template_name, 0) + 1

            if verbose:
                click.echo(click.style(f"    -> {template_name}: score={score:.1f}, records={num_records}", fg='green'))

            # Write output file
            if not dry_run and parsed_data:
                # Build output filename
                output_filename = file_path.stem.replace('._output', '') + '.json'
                if output_filename.startswith('.'):
                    output_filename = file_path.name.replace('._output', '.json')

                # Handle nested subdirectories
                file_rel = file_path.relative_to(capture_path / folder_name)
                if len(file_rel.parts) > 1:
                    nested_output = folder_output_path / file_rel.parent
                    nested_output.mkdir(parents=True, exist_ok=True)
                    output_file = nested_output / output_filename
                else:
                    output_file = folder_output_path / output_filename

                # Create result object with metadata
                result = {
                    'source_file': str(rel_path),
                    'template': template_name,
                    'score': round(score, 2),
                    'record_count': num_records,
                    'parsed_at': datetime.now().isoformat(),
                    'data': parsed_data
                }

                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=2, default=str)

        # Calculate folder average score
        if folder_stats['scores']:
            folder_stats['avg_score'] = sum(folder_stats['scores']) / len(folder_stats['scores'])

        stats.folder_stats[folder_name] = folder_stats

        # Folder summary
        click.echo(f"  ✓ {folder_stats['matched']}/{folder_stats['total']} matched, "
                   f"{folder_stats['records']} records" +
                   (f", avg score: {folder_stats['avg_score']:.1f}" if folder_stats['avg_score'] > 0 else ""))

    stats.processing_time = time.time() - start_time

    # Print summary
    click.echo()
    click.echo(click.style("=" * 70, fg='cyan'))
    click.echo(click.style("PROCESSING SUMMARY", fg='cyan', bold=True))
    click.echo(click.style("=" * 70, fg='cyan'))

    click.echo(f"\nFiles processed:     {stats.processed}/{stats.total_files}")
    click.echo(click.style(f"Successfully parsed: {stats.matched}", fg='green'))
    click.echo(f"Below threshold:     {stats.below_threshold}")
    click.echo(f"No match found:      {stats.failed}")
    click.echo(f"Skipped (empty):     {stats.skipped_empty}")
    click.echo(f"\nTotal records:       {stats.total_records}")

    if stats.scores:
        avg_score = sum(stats.scores) / len(stats.scores)
        min_s = min(stats.scores)
        max_s = max(stats.scores)
        click.echo(f"Score range:         {min_s:.1f} - {max_s:.1f} (avg: {avg_score:.1f})")

    click.echo(f"\nProcessing time:     {stats.processing_time:.1f}s")
    click.echo(f"Rate:                {stats.processed / stats.processing_time:.1f} files/sec")

    # Top templates
    if stats.template_hits:
        click.echo(click.style("\nTop Templates Used:", fg='yellow'))
        sorted_templates = sorted(stats.template_hits.items(), key=lambda x: x[1], reverse=True)
        for template, count in sorted_templates[:15]:
            click.echo(f"  {count:4d}x  {template}")

    # Per-folder breakdown
    click.echo(click.style("\nPer-Folder Results:", fg='yellow'))
    click.echo(f"{'Folder':<25} {'Matched':>8} {'Total':>8} {'Records':>10} {'Avg Score':>10}")
    click.echo("-" * 65)
    for folder, fs in sorted(stats.folder_stats.items(), key=lambda x: x[1]['matched'], reverse=True):
        if fs['total'] > 0:
            click.echo(f"{folder:<25} {fs['matched']:>8} {fs['total']:>8} {fs['records']:>10} {fs['avg_score']:>10.1f}")

    if not dry_run:
        click.echo(f"\nResults written to: {output_path}")

    click.echo()


if __name__ == '__main__':
    main()