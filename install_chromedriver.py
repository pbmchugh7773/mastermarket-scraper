#!/usr/bin/env python3
"""
ChromeDriver Installation Script for GitHub Actions
Finds and installs the actual ChromeDriver executable from webdriver-manager
"""

from webdriver_manager.chrome import ChromeDriverManager
import os
import shutil
import sys

def find_chromedriver_executable(base_path):
    """Find the actual chromedriver executable from webdriver-manager path"""
    print(f'Base path from webdriver-manager: {base_path}')
    
    if os.path.isdir(base_path):
        # Look for chromedriver executable (not THIRD_PARTY_NOTICES)
        chromedriver_files = []
        for root, dirs, files in os.walk(base_path):
            for file in files:
                if file == 'chromedriver' and not file.endswith('.chromedriver'):
                    full_path = os.path.join(root, file)
                    # Verify it's an executable, not a text file
                    if os.path.isfile(full_path) and os.access(full_path, os.X_OK):
                        chromedriver_files.append(full_path)
        
        if chromedriver_files:
            return chromedriver_files[0]
        else:
            print('No executable chromedriver found, trying alternative locations...')
            # Try parent directory
            parent_dir = os.path.dirname(base_path)
            alt_paths = [
                os.path.join(parent_dir, 'chromedriver-linux64', 'chromedriver'),
                os.path.join(os.path.dirname(parent_dir), 'chromedriver-linux64', 'chromedriver')
            ]
            driver_path = next((p for p in alt_paths if os.path.isfile(p) and os.access(p, os.X_OK)), None)
            
            if not driver_path:
                print(f'Error: No executable chromedriver found in {base_path} or alternatives')
                return None
            return driver_path
    else:
        # base_path is a file, check if it's the actual chromedriver or find the real one
        if base_path.endswith('chromedriver') and 'THIRD_PARTY' not in base_path:
            return base_path
        elif 'THIRD_PARTY_NOTICES.chromedriver' in base_path:
            # webdriver-manager returned the wrong file, find the actual chromedriver
            parent_dir = os.path.dirname(base_path)
            print(f'Looking for chromedriver in: {parent_dir}')
            
            # List all files in the directory for debugging
            if os.path.exists(parent_dir):
                files = os.listdir(parent_dir)
                print(f'Files in directory: {files}')
            
            chromedriver_path = os.path.join(parent_dir, 'chromedriver')
            if os.path.exists(chromedriver_path) and os.access(chromedriver_path, os.X_OK):
                return chromedriver_path
            else:
                print(f'Error: Actual chromedriver not found in {parent_dir}')
                # Try to find any chromedriver file recursively
                for root, dirs, files in os.walk(parent_dir):
                    for file in files:
                        if file == 'chromedriver' and os.access(os.path.join(root, file), os.X_OK):
                            found_path = os.path.join(root, file)
                            print(f'Found chromedriver at: {found_path}')
                            return found_path
                return None
        else:
            print(f'Error: Invalid chromedriver path: {base_path}')
            return None

def main():
    try:
        # Get the base directory path from webdriver-manager
        base_path = ChromeDriverManager().install()
        
        # Find the actual chromedriver executable
        driver_path = find_chromedriver_executable(base_path)
        
        if not driver_path:
            sys.exit(1)
        
        print(f'Using ChromeDriver at: {driver_path}')
        
        # Verify the file is executable and copy it
        if os.path.exists(driver_path) and os.access(driver_path, os.X_OK):
            shutil.copy2(driver_path, '/tmp/chromedriver')
            os.chmod('/tmp/chromedriver', 0o755)
            print('ChromeDriver copied successfully to /tmp/chromedriver')
        else:
            print(f'Error: ChromeDriver not found or not executable at {driver_path}')
            sys.exit(1)
            
    except Exception as e:
        print(f'Error installing ChromeDriver: {e}')
        sys.exit(1)

if __name__ == '__main__':
    main()