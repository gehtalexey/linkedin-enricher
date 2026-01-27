"""
LinkedIn Profile Enricher using Crust Data API
Usage: python enrich.py input_file.csv (or input_file.json)
"""

import sys
import json
import csv
import time
import requests
from pathlib import Path


def load_urls(file_path: str) -> list[str]:
    """Load LinkedIn URLs from CSV or JSON file."""
    path = Path(file_path)

    if path.suffix.lower() == '.json':
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Handle both list of URLs and list of objects with 'url' key
            if isinstance(data, list):
                if len(data) > 0 and isinstance(data[0], dict):
                    return [item.get('url') or item.get('linkedin_url') or item.get('profile_url') or item.get('public_url') for item in data]
                return data
            return []

    elif path.suffix.lower() == '.csv':
        urls = []
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Try common column names
                url = (row.get('url') or row.get('linkedin_url') or
                       row.get('profile_url') or row.get('URL') or
                       row.get('LinkedIn URL') or row.get('linkedinUrl') or
                       row.get('public_url'))
                if url:
                    urls.append(url)
        return urls

    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")


def enrich_profiles(urls: list[str], api_key: str, batch_size: int = 10) -> list[dict]:
    """Call Crust Data API to enrich LinkedIn profiles."""
    all_results = []

    # Process in batches
    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]
        batch_str = ','.join(batch)

        print(f"Processing batch {i // batch_size + 1} ({len(batch)} profiles)...")

        try:
            response = requests.get(
                'https://api.crustdata.com/screener/person/enrich',
                params={'linkedin_profile_url': batch_str},
                headers={'Authorization': f'Token {api_key}'},
                timeout=120
            )

            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    all_results.extend(data)
                else:
                    all_results.append(data)
            else:
                print(f"  Error: {response.status_code} - {response.text}")
                # Add empty results for failed URLs
                for url in batch:
                    all_results.append({'linkedin_url': url, 'error': response.text})

        except Exception as e:
            print(f"  Error: {e}")
            for url in batch:
                all_results.append({'linkedin_url': url, 'error': str(e)})

        # Rate limiting - wait between batches
        if i + batch_size < len(urls):
            time.sleep(2)

    return all_results


def flatten_profile(profile: dict) -> dict:
    """Flatten nested profile data for CSV export."""
    flat = {}

    for key, value in profile.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, (list, dict)):
                    flat[f"{key}_{sub_key}"] = json.dumps(sub_value)
                else:
                    flat[f"{key}_{sub_key}"] = sub_value
        elif isinstance(value, list):
            flat[key] = json.dumps(value)
        else:
            flat[key] = value

    return flat


def save_results(results: list[dict], output_base: str):
    """Save results to both JSON and CSV."""
    # Save JSON
    json_path = f"{output_base}_enriched.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Saved JSON: {json_path}")

    # Save CSV
    csv_path = f"{output_base}_enriched.csv"
    if results:
        flat_results = [flatten_profile(r) for r in results]
        # Get all possible keys
        all_keys = set()
        for r in flat_results:
            all_keys.update(r.keys())

        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=sorted(all_keys))
            writer.writeheader()
            writer.writerows(flat_results)
    print(f"Saved CSV: {csv_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python enrich.py <input_file.csv or input_file.json>")
        print("\nExample:")
        print("  python enrich.py urls.csv")
        print("  python enrich.py urls.json")
        sys.exit(1)

    input_file = sys.argv[1]

    # Load API key from environment or config
    api_key = None
    config_path = Path(__file__).parent / 'config.json'

    if config_path.exists():
        with open(config_path, 'r') as f:
            config = json.load(f)
            api_key = config.get('api_key')

    if not api_key:
        import os
        api_key = os.environ.get('CRUSTDATA_API_KEY')

    if not api_key:
        print("Error: No API key found.")
        print("Either:")
        print("  1. Create config.json with: {\"api_key\": \"your_key_here\"}")
        print("  2. Set CRUSTDATA_API_KEY environment variable")
        sys.exit(1)

    # Load URLs
    print(f"Loading URLs from {input_file}...")
    urls = load_urls(input_file)
    urls = [u for u in urls if u]  # Remove empty
    print(f"Found {len(urls)} URLs")

    if not urls:
        print("No URLs found in input file.")
        sys.exit(1)

    # Enrich
    print("\nEnriching profiles via Crust Data API...")
    results = enrich_profiles(urls, api_key)

    # Save
    output_base = Path(input_file).stem
    save_results(results, output_base)

    print(f"\nDone! Enriched {len(results)} profiles.")


if __name__ == '__main__':
    main()
