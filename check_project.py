import os
import sys

print("=== Project State Check ===")

# Check if critical directories exist
expected_dirs = ['src/preflight/api', 'src/preflight/core', 'tests', 'docs']

for dir_path in expected_dirs:
    if os.path.exists(dir_path):
        print(f"✅ {dir_path}/")
        # List files in directory
        if os.path.isdir(dir_path):
            files = os.listdir(dir_path)
            if files:
                print(f"   Files: {files}")
            else:
                print(f"   Files: (empty)")
    else:
        print(f"❌ {dir_path}/ (missing)")

# Check specific critical files
critical_files = [
    'src/preflight/api/app.py',
    'src/preflight/api/payments.py',
    'src/preflight/api/cache.py',
    'src/preflight/api/ratelimit.py',
    'src/preflight/core/pipeline.py',
    'tests/test_api_basic.py',
    'tests/test_api_complete.py',
    'docs/STATUS_api.md'
]

print("\n=== Critical Files Check ===")
for file in critical_files:
    if os.path.exists(file):
        print(f"✅ {file}")
    else:
        print(f"❌ {file} (missing)")
