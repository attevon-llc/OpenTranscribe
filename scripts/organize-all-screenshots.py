#!/usr/bin/env python3
"""
Complete Screenshot Organization Script - All 104 Screenshots
Processes all OpenTranscribe screenshots with comprehensive mapping
"""

import json
import os
from pathlib import Path

from PIL import Image

# Configuration
SOURCE_DIR = 'OpenTranscribe-Screenshots'
TARGET_DIR = 'docs-site/static/img/screenshots'
MAX_WIDTH = 1920
QUALITY = 85


def get_file_size_kb(file_path):
    """Get file size in kilobytes."""
    return os.path.getsize(file_path) / 1024


def optimize_image(input_path, output_path, max_width=MAX_WIDTH, quality=QUALITY):
    """Optimize image by resizing and compressing."""
    try:
        with Image.open(input_path) as img:
            # Convert RGBA to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background

            # Resize if wider than max_width
            if img.width > max_width:
                ratio = max_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

            # Save with optimization
            img.save(output_path, 'PNG', optimize=True, quality=quality)

            file_size_kb = get_file_size_kb(output_path)

            # Further optimize if still too large
            if file_size_kb > 200:
                adjusted_quality = max(70, int(quality * (150 / file_size_kb)))
                img.save(output_path, 'PNG', optimize=True, quality=adjusted_quality)

            return True, get_file_size_kb(output_path)
    except Exception as e:
        return False, str(e)


def main():
    # Load the remaining screenshots mapping
    with open('scripts/remaining-screenshots-map.json') as f:
        screenshot_map = json.load(f)

    source_path = Path(SOURCE_DIR)
    target_path = Path(TARGET_DIR)

    # Create category directories
    categories = set()
    for data in screenshot_map.values():
        categories.add(data['category'])

    for category in categories:
        (target_path / category).mkdir(parents=True, exist_ok=True)

    # Process screenshots
    metadata = {'total_processed': 0, 'total_skipped': 0, 'screenshots': []}

    processed_count = 0
    skipped_count = 0

    for screenshot_file in sorted(source_path.glob('Screenshot*.png')):
        found = False

        # Find matching entry in screenshot_map
        for key, data in screenshot_map.items():
            if key in screenshot_file.name:
                category = data['category']
                name = data['name']
                description = data['description']

                # Create sequential numbering
                existing_files = list((target_path / category).glob('*.png'))
                file_number = len(existing_files) + 1
                output_filename = f'{file_number:02d}-{name}.png'
                output_file = target_path / category / output_filename

                print(f'Processing: {screenshot_file.name}')
                print(f'  -> {category}/{output_filename}')

                success, result = optimize_image(str(screenshot_file), str(output_file))

                if success:
                    file_size_kb = result
                    print(f'  ✓ Optimized: {file_size_kb:.1f} KB')

                    metadata['screenshots'].append(
                        {
                            'original_name': screenshot_file.name,
                            'category': category,
                            'filename': output_filename,
                            'path': f'/img/screenshots/{category}/{output_filename}',
                            'description': description,
                            'size_kb': round(file_size_kb, 1),
                        }
                    )

                    processed_count += 1
                else:
                    print(f'  ✗ Failed: {result}')
                    skipped_count += 1

                found = True
                break

        if not found:
            print(f'Skipping: {screenshot_file.name} (not in mapping)')
            skipped_count += 1

    metadata['total_processed'] = processed_count
    metadata['total_skipped'] = skipped_count

    # Save metadata
    metadata_file = target_path / 'screenshots-metadata-complete.json'
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=2)

    print('\n✅ Processing complete!')
    print(f'   Processed: {processed_count} screenshots')
    print(f'   Skipped: {skipped_count} screenshots')
    print(f'   Metadata: {metadata_file}')


if __name__ == '__main__':
    main()
